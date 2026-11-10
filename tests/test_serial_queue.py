import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'bin' / 'serial_queue.py'
spec = importlib.util.spec_from_file_location('queue_runner', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SerialQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'labtech'
        self.root = self.home / 'queue'
        self.root.mkdir(parents=True)
        self.source = self.base / 'source'
        self.source.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'empty', '--allow-empty'], check=True)
        self.revision = m.capture(['git', '-C', str(self.source), 'rev-parse', 'HEAD'])
        self.store = self.base / 'beads.json'
        m.write(self.store, {})
        self.gc = self.base / 'gc-fake'
        self.gc.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
store=pathlib.Path(__file__).with_name('beads.json')
data=json.loads(store.read_text())
a=sys.argv[1:]
if 'nudge' in a:
    print('notified'); sys.exit(0)
a=a[a.index('bd')+1:]
if a[0]=='--rig': a=a[2:]
print('configuration diagnostic on stderr',file=sys.stderr)
cmd, rid = a[:2]
if cmd=='show':
    print(json.dumps([data[rid]])); sys.exit(0)
if cmd=='update':
    for i,v in enumerate(a):
        if v=='--set-metadata':
            k,val=a[i+1].split('=',1)
            data[rid].setdefault('metadata',{})[k]=val
    store.write_text(json.dumps(data)); print('updated'); sys.exit(0)
raise Exception(a)
''')
        self.gc.chmod(0o755)
        m.write(self.root / 'config.json', {'city': str(self.base), 'rig': 'test', 'target': 'test/george',
                'reviewers': ['test/george'], 'gc': str(self.gc), 'machine': m.host()['machine'], 'lab_book': 'lab'})
        (self.root / 'requests').mkdir()
        (self.root / 'attempts').mkdir()
        self.env = patch.dict(os.environ, {'GC_AGENT': 'test/george'})
        self.env.start()
        self.q = m.Queue(self.home)
        self.script = self.base / 'experiment.py'
        self.script.write_text('''import json,os,pathlib,sys,time
out=pathlib.Path(os.environ['LABTECH_RESULTS_DIR'])
assert all((out.parent/name).is_file() for name in ('record.json','provenance.json','start.json'))
assert not any(k.startswith('GC_') for k in os.environ)
time.sleep(float(sys.argv[1]))
(out/'outcome.json').write_text(json.dumps({'measured_outcome': {'count': 42}}))
print('ran',flush=True)
sys.exit(int(sys.argv[2]))
''')
        self.spec = {'argv': [sys.executable, str(self.script), '.1', '0'], 'cwd': str(self.source),
                     'source': {'path': str(self.source), 'revision': self.revision},
                     'runtime': {'argv': [sys.executable, '--version'], 'expected': 'Python'},
                     'parameters': {}, 'inputs': [str(self.script)], 'scope_beads': [{'rig': 'test', 'id': 'scope'}],
                     'purpose': 'bounded harmless test', 'worktree_owner': 'test/george'}
        self.bead('scope')
        self.bead('authorization')
        self.bead('city-authorization')

    def tearDown(self):
        # Only task-owned child process groups are touched, and only for test cleanup.
        for identity in self.root.glob('attempts/*/identity.json'):
            data = m.read(identity)
            if m.process(data['pid']) == data['ps']:
                os.killpg(data['pgid'], signal.SIGKILL)
        self.env.stop()
        self.tmp.cleanup()

    def bead(self, rid, status='open', metadata=None):
        data = m.read(self.store)
        data[rid] = {'id': rid, 'status': status, 'metadata': metadata or {}}
        m.write(self.store, data)

    def queue(self, rid='request', spec=None):
        self.bead(rid)
        record = self.q.submit(rid, spec or self.spec)
        self.q.approve(rid, {'rig': 'test', 'id': 'authorization'})
        return record

    def await_receipt(self, aid):
        path = self.root / 'attempts' / aid / 'exit.json'
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.exists():
                # Supervisor closes its lock after recording notification.
                for _ in range(100):
                    result = self.q.reconcile()
                    if result['active'] is None:
                        return m.read(path)
                    time.sleep(.02)
            time.sleep(.03)
        logs = list(self.root.glob('attempts/*/supervisor.log'))
        self.fail('no completion: ' + '\n'.join(p.read_text() for p in logs))

    def test_serial_restart_receipt_and_no_completed_rerun(self):
        slow = dict(self.spec, argv=[sys.executable, str(self.script), '.7', '0'])
        self.queue('first', slow)
        self.queue('second')
        a = self.q.run_next()
        self.assertTrue(a['launched'])
        self.assertTrue((Path(a['record'])).exists())
        resumed = m.Queue(self.home)
        self.assertFalse(resumed.run_next()['launched'])
        receipt = self.await_receipt(a['attempt_id'])
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['outcome']['measured_outcome']['count'], 42)
        b = resumed.run_next()
        self.assertTrue(b['launched'])
        self.await_receipt(b['attempt_id'])
        self.assertFalse(resumed.run_next()['launched'])
        self.assertEqual(len(resumed.attempts()), 2)
        self.assertEqual(self.q.bead({'rig': '', 'id': 'city-authorization'})['id'], 'city-authorization')
        for path in resumed.attempts():
            evidence = m.read(path.parent / 'provenance.json')
            self.assertEqual(evidence['source']['revision'], self.revision)
            self.assertTrue(evidence['inputs'][0]['snapshot'])
            self.assertNotIn('GC_AGENT', evidence['inherited_environment_keys'])

    def test_competing_launchers_only_one_wins(self):
        slow = dict(self.spec, argv=[sys.executable, str(self.script), '.7', '0'])
        self.queue('one', slow)
        self.queue('two', slow)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda _: m.Queue(self.home).run_next(), range(2)))
        self.assertEqual(sum(a['launched'] for a in answers), 1)
        self.await_receipt(next(a['attempt_id'] for a in answers if a['launched']))
        self.assertEqual(len(self.q.attempts()), 1)

    def test_failed_attempt_retained_explicit_retry_link(self):
        failed = dict(self.spec, argv=[sys.executable, str(self.script), '.01', '7'])
        self.queue(spec=failed)
        a = self.q.run_next()
        self.assertEqual(self.await_receipt(a['attempt_id'])['exit_code'], 7)
        self.assertFalse(self.q.run_next()['launched'])
        self.q.retry('request', {'rig': 'test', 'id': 'authorization'})
        b = self.q.run_next()
        self.await_receipt(b['attempt_id'])
        self.assertEqual(m.read(b['record'])['previous_attempt_id'], a['attempt_id'])
        self.assertTrue((Path(a['record']).parent / 'exit.json').exists())

    def test_hold_and_authorization_digest(self):
        self.queue()
        self.bead('scope', 'blocked')
        self.assertFalse(self.q.run_next()['launched'])
        self.bead('scope')
        path = self.root / 'requests/request/spec.json'
        changed = m.read(path)
        changed['parameters'] = {'N': 200}
        m.write(path, changed)
        self.assertFalse(self.q.run_next()['launched'])
        self.assertEqual(len(self.q.attempts()), 0)

    def test_changed_input_fails_before_experiment(self):
        self.queue()
        self.script.write_text('raise Exception("MUST NOT EXECUTE")')
        a = self.q.run_next()
        receipt = self.await_receipt(a['attempt_id'])
        self.assertEqual(receipt['exit_code'], 125)
        self.assertIn('changed since approval', receipt['error'])
        self.assertFalse((Path(a['record']).parent / 'start.json').exists())

    def test_ambiguous_reservation_never_launches(self):
        self.queue()
        m.write(self.root / 'active.json', {'attempt_id': 'lost.a1', 'token': 'lost'})
        (self.root / 'attempts/lost.a1').mkdir()
        checkpoint = self.q.reconcile()
        self.assertIn('uncertain', checkpoint['active']['observation'])
        self.assertFalse(self.q.run_next()['launched'])

    def test_foreign_host_and_invalid_paths_refuse(self):
        config = m.read(self.root / 'config.json')
        config['machine'] = 'different-host'
        m.write(self.root / 'config.json', config)
        with self.assertRaisesRegex(ValueError, 'another host'):
            m.Queue(self.home)
        with self.assertRaises(ValueError):
            m.label('../escape')

    def test_pending_review_and_changed_source(self):
        self.bead('request')
        self.q.submit('request', self.spec)
        self.assertFalse(self.q.run_next()['launched'])
        self.q.approve('request', {'rig': 'test', 'id': 'authorization'})
        (self.source / 'dirty').write_text('changed source')
        a = self.q.run_next()
        self.assertEqual(self.await_receipt(a['attempt_id'])['exit_code'], 125)

    def test_killed_supervisor_leaves_lock_with_child_and_needs_review(self):
        slow = dict(self.spec, argv=[sys.executable, str(self.script), '1', '0'])
        self.queue(spec=slow)
        a = self.q.run_next()
        folder = Path(a['record']).parent
        deadline = time.monotonic() + 10
        while not (folder / 'child.json').exists() and time.monotonic() < deadline:
            time.sleep(.03)
        identity = m.read(folder / 'identity.json')
        os.kill(identity['pid'], signal.SIGKILL)
        self.assertFalse(self.q.run_next()['launched'])
        evidence = self.base / 'review.txt'
        evidence.write_text('Task-owned supervisor was killed in test; await inherited lock release and verify process group gone.')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                result = self.q.resolve_interrupted(a['attempt_id'], {'rig': 'test', 'id': 'authorization'}, evidence)
                break
            except (BlockingIOError, ValueError):
                time.sleep(.05)
        else:
            self.fail('interruption could not be resolved')
        self.assertIsNone(result['active'])
        receipt = m.read(folder / 'exit.json')
        self.assertIsNone(receipt['exit_code'])
        self.assertEqual(receipt['status'], 'interrupted')
        self.assertFalse(self.q.run_next()['launched'])


if __name__ == '__main__':
    unittest.main()
