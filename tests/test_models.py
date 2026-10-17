"""Run every model over its fixture cases in DuckDB, compare the rows, and check the output against the data contract."""

import json
from pathlib import Path

import pytest
import yaml

from bqci import load_catalog
from bqci.contracts import check, load_contract
from bqci.duck import as_dicts, run_model

ROOT = Path(__file__).resolve().parent.parent
CATALOG = load_catalog()


def all_cases():
    for path in sorted((ROOT / "tests" / "cases").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            yield pytest.param(doc["model"], case, id=f"{doc['model']}: {case['name']}")


def canonical(rows):
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


@pytest.mark.parametrize("model,case", list(all_cases()))
def test_the_model_produces_exactly_the_expected_rows(model, case):
    sql = (ROOT / "sql" / f"{model}.sql").read_text(encoding="utf-8")
    cols, types, rows = run_model(sql, CATALOG, case.get("given") or {}, case["params"])
    assert canonical(as_dicts(cols, rows)) == canonical(case["expect"])


@pytest.mark.parametrize("model,case", list(all_cases()))
def test_every_case_output_honours_the_data_contract(model, case):
    sql = (ROOT / "sql" / f"{model}.sql").read_text(encoding="utf-8")
    cols, types, rows = run_model(sql, CATALOG, case.get("given") or {}, case["params"])
    assert check(load_contract(model), cols, types, rows) == []


def test_every_model_has_cases_and_a_contract():
    models = {p.stem for p in (ROOT / "sql").glob("*.sql")}
    with_cases = {yaml.safe_load(p.read_text(encoding="utf-8"))["model"] for p in (ROOT / "tests" / "cases").glob("*.yaml")}
    with_contract = {p.stem for p in (ROOT / "contracts").glob("*.yaml")}
    assert models == with_cases == with_contract
    for m in models:
        n = sum(1 for p in all_cases() if p.values[0] == m)
        assert n >= 6, f"{m} has only {n} cases"


def test_a_missing_parameter_is_an_error_not_a_silent_null():
    sql = (ROOT / "sql" / "daily_revenue.sql").read_text(encoding="utf-8")
    with pytest.raises(KeyError, match="run_date"):
        run_model(sql, CATALOG, {}, {})
