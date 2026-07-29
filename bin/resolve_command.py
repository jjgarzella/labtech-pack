#!/usr/bin/env python3
"""resolve_command.py — render a Lab Tech catalog `command` into a concrete line.

This is the pack-owned RESOLUTION step (Q4/Q5 OPTION A). The catalog
(`experiments.toml`, rig-local) is the MENU of runnable experiments; each
`[[experiment]]` carries a Go-text/template `command` with `{{.param}}` / `{{.run_id}}`
holes plus `[experiment.constants]` and tunable `[[experiment.parameter]]` blocks.

Given an experiment name, a run_id, and chosen parameter values, this tool builds the
template data map by MERGING, in order:

    [experiment.constants]   -> n, d        (fixed, not tunable)
    chosen --param values    -> p, N        (Lab Tech picks; `default` is the seed)
    run_id                   -> run_id

renders the `command` against that map, and prints the resolved string. The tick
formula hands that string straight to `dispatch`. Keeping this in one small,
testable tool — instead of the wisp eyeballing the template — makes `gc order run`
deterministic and removes a class of mis-substitution error.

Networking stays OUT of here (Q7): this only renders text. It never talks to a box or DB.

Subcommands
-----------
  list   --catalog C
      Print the experiment names in the catalog, one per line.

  show   --catalog C --experiment NAME [--json]
      Print the experiment's menu entry (name, table, estimated_duration, the command
      template, constants, and each tunable parameter's name/type/default/description).
      `--json` emits a machine-readable object; default is a human summary.

  render --catalog C --experiment NAME --run-id RID [-p key=value ...]
      Print the resolved command line. Unspecified tunable parameters fall back to their
      catalog `default`. Errors (nonzero exit) if a referenced hole is unfilled, if a
      --param names something that is neither a parameter nor a constant, or if the
      catalog/experiment is missing.

The Go-template subset supported is exactly what the v1 catalog uses: `{{.key}}` holes
with optional surrounding whitespace. No conditionals/ranges/pipelines (the catalog
never uses them); anything fancier is rejected so a silent mis-render can't happen.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib

# A single template hole: {{ .key }} with optional inner whitespace.
_HOLE = re.compile(r"\{\{\s*\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Anything that looks like template machinery but is NOT a bare `{{.key}}` hole — we
# refuse to render these rather than pass them through unsubstituted.
_FUNKY = re.compile(r"\{\{(?!\s*\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}).*?\}\}")


def die(msg: str) -> "None":
    print(f"resolve_command: {msg}", file=sys.stderr)
    sys.exit(2)


def load_catalog(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        die(f"catalog not found: {path}")
    except tomllib.TOMLDecodeError as e:
        die(f"catalog {path} is not valid TOML: {e}")


def experiments(catalog: dict) -> list:
    exps = catalog.get("experiment")
    if not exps:
        die("catalog has no [[experiment]] entries")
    if isinstance(exps, dict):  # a lone [[experiment]] parses to a dict in some tomls
        exps = [exps]
    return exps


def find_experiment(catalog: dict, name: str) -> dict:
    for exp in experiments(catalog):
        if exp.get("name") == name:
            return exp
    have = ", ".join(e.get("name", "?") for e in experiments(catalog))
    die(f"no experiment named {name!r} in catalog (have: {have})")


def parameters(exp: dict) -> list:
    params = exp.get("parameter", [])
    if isinstance(params, dict):
        params = [params]
    return params


def cmd_list(args) -> None:
    for exp in experiments(load_catalog(args.catalog)):
        print(exp.get("name", ""))


def cmd_show(args) -> None:
    exp = find_experiment(load_catalog(args.catalog), args.experiment)
    info = {
        "name": exp.get("name"),
        "table": exp.get("table"),
        "estimated_duration": exp.get("estimated_duration"),
        "command": exp.get("command"),
        "description": (exp.get("description") or "").strip(),
        "guidance": (exp.get("guidance") or "").strip(),
        "constants": exp.get("constants", {}),
        "parameters": [
            {
                "name": p.get("name"),
                "type": p.get("type"),
                "default": p.get("default"),
                "description": (p.get("description") or "").strip(),
            }
            for p in parameters(exp)
        ],
    }
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
        return
    print(f"experiment : {info['name']}")
    print(f"table      : {info['table']}")
    print(f"duration   : {info['estimated_duration']}")
    print(f"constants  : {info['constants']}")
    for p in info["parameters"]:
        print(f"parameter  : {p['name']} (type={p['type']}, default={p['default']})")
    print(f"command    : {info['command']}")


def parse_param(kv: str) -> tuple:
    if "=" not in kv:
        die(f"--param must be key=value, got {kv!r}")
    k, v = kv.split("=", 1)
    k = k.strip()
    if not k:
        die(f"--param has empty key: {kv!r}")
    return k, v.strip()


def cmd_render(args) -> None:
    exp = find_experiment(load_catalog(args.catalog), args.experiment)
    template = exp.get("command")
    if not template:
        die(f"experiment {args.experiment!r} has no `command`")
    if _FUNKY.search(template):
        die("command template uses unsupported text/template syntax "
            "(only bare {{.key}} holes are allowed)")

    constants = dict(exp.get("constants", {}))
    param_defaults = {}
    known_params = set()
    for p in parameters(exp):
        nm = p.get("name")
        if nm is None:
            die("a [[parameter]] block is missing `name`")
        known_params.add(nm)
        if "default" in p:
            param_defaults[nm] = p["default"]

    # Chosen values override defaults; reject params that are neither a tunable
    # parameter nor a constant (a typo'd hole would otherwise dispatch garbage).
    chosen = {}
    for kv in args.param or []:
        k, v = parse_param(kv)
        if k not in known_params and k not in constants:
            die(f"--param {k!r} is not a parameter or constant of {args.experiment!r}")
        chosen[k] = v

    data: dict = {}
    data.update(constants)
    data.update(param_defaults)
    data.update(chosen)
    data["run_id"] = args.run_id

    missing: list = []

    def sub(m: "re.Match") -> str:
        key = m.group(1)
        if key not in data or data[key] is None or data[key] == "":
            missing.append(key)
            return m.group(0)
        return str(data[key])

    rendered = _HOLE.sub(sub, template)
    if missing:
        die(f"unfilled template hole(s): {', '.join(sorted(set(missing)))}")
    print(rendered)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a Lab Tech catalog command.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list experiment names")
    pl.add_argument("--catalog", required=True)
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="show one experiment's menu entry")
    ps.add_argument("--catalog", required=True)
    ps.add_argument("--experiment", required=True)
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_show)

    pr = sub.add_parser("render", help="render the resolved command line")
    pr.add_argument("--catalog", required=True)
    pr.add_argument("--experiment", required=True)
    pr.add_argument("--run-id", required=True)
    pr.add_argument("-p", "--param", action="append", metavar="key=value",
                    help="chosen parameter value; repeatable")
    pr.set_defaults(func=cmd_render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
