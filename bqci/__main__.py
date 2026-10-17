"""python -m bqci lint [DIR] | cost [DIR] | test

lint   static rules BQ001-BQ008 on every .sql file; exits 1 on any error
cost   estimated bytes scanned per model and whether it is within its own `max_gib` budget; exits 1 if over
test   runs every model over its fixture cases in DuckDB and checks the data contract; exits 1 on any failure
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .catalog import ROOT, load_catalog
from .contracts import check, load_contract
from .cost import GIB, budget_problem, estimate
from .duck import as_dicts, run_model
from .lint import lint


def _models(directory: Path):
    return sorted(directory.glob("*.sql"))


def cmd_lint(directory: Path) -> int:
    cat = load_catalog()
    errors = 0
    for f in _models(directory):
        findings = lint(f.read_text(encoding="utf-8"), cat)
        for x in findings:
            print(f"{f.name}: {x.severity.upper():<7} {x.rule}  {x.message}")
            errors += x.severity == "error"
        if not findings:
            print(f"{f.name}: ok")
    print(f"{errors} error(s)")
    return 1 if errors else 0


def cmd_cost(directory: Path) -> int:
    cat = load_catalog()
    bad = 0
    print(f"{'model':<18}{'scanned GiB':>12}{'unpruned GiB':>14}  budget")
    for f in _models(directory):
        sql = f.read_text(encoding="utf-8")
        e = estimate(sql, cat)
        problem = budget_problem(sql, e)
        bad += problem is not None
        print(f"{f.stem:<18}{e.scanned_bytes / GIB:>12.2f}{e.full_bytes / GIB:>14.1f}  {problem or 'within budget'}")
    return 1 if bad else 0


def cmd_test() -> int:
    cat = load_catalog()
    failed = total = 0
    for path in sorted((ROOT / "tests" / "cases").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        sql = (ROOT / "sql" / f"{doc['model']}.sql").read_text(encoding="utf-8")
        contract = load_contract(doc["model"])
        for case in doc["cases"]:
            total += 1
            cols, types, rows = run_model(sql, cat, case.get("given") or {}, case["params"])
            got = sorted(map(repr, (sorted(r.items()) for r in as_dicts(cols, rows))))
            want = sorted(map(repr, (sorted(r.items()) for r in case["expect"])))
            problems = check(contract, cols, types, rows)
            ok = got == want and not problems
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {doc['model']}: {case['name']}")
            for p in problems:
                print("       contract:", p)
    print(f"{total - failed}/{total} cases passed")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="bqci")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("lint", "cost"):
        s = sub.add_parser(name)
        s.add_argument("directory", nargs="?", type=Path, default=ROOT / "sql")
    sub.add_parser("test")
    a = p.parse_args()
    return {"lint": lambda: cmd_lint(a.directory), "cost": lambda: cmd_cost(a.directory), "test": cmd_test}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
