"""Data contracts: what a model's output must look like, checked against the result of running it on fixtures.

A contract lists the columns in order with a type family and whether NULL is allowed, plus the column set that must be
unique. Downstream dashboards and models depend on this shape, so a change that breaks it should fail in CI, not in
somebody's report.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .catalog import ROOT

# BigQuery type -> the DuckDB result type names that are acceptable for it.
FAMILIES = {
    "STRING": ("VARCHAR",),
    "INT64": ("BIGINT", "HUGEINT", "INTEGER"),
    # Known divergence: BigQuery keeps NUMERIC through division (SAFE_DIVIDE(NUMERIC, INT64) is NUMERIC) but DuckDB computes
    # it as DOUBLE. So in DuckDB a NUMERIC column may legitimately come back as DOUBLE, and this check cannot tell a
    # computed NUMERIC from a FLOAT64. Only a dry run on real BigQuery can.
    "NUMERIC": ("DECIMAL", "DOUBLE"),
    "FLOAT64": ("DOUBLE", "FLOAT"),
    "BOOL": ("BOOLEAN",),
    "DATE": ("DATE",),
    "TIMESTAMP": ("TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"),
}


def load_contract(model: str, directory: Path = ROOT / "contracts") -> dict:
    return yaml.safe_load((directory / f"{model}.yaml").read_text(encoding="utf-8"))


def check(contract: dict, cols: list[str], types: list[str], rows: list[list]) -> list[str]:
    problems: list[str] = []
    want = [c["name"] for c in contract["columns"]]
    if cols != want:
        problems.append(f"columns are {cols}, the contract says {want}")
        return problems  # the rest is meaningless if the shape is wrong
    for i, spec in enumerate(contract["columns"]):
        family = FAMILIES[spec["type"]]
        if not any(types[i].upper().startswith(f) for f in family):
            problems.append(f"{spec['name']} is {types[i]}, the contract says {spec['type']}")
        if not spec.get("nullable", False) and any(r[i] is None for r in rows):
            problems.append(f"{spec['name']} is declared not-null but the output contains NULL")
    unique = contract.get("unique", [])
    if unique:
        idx = [cols.index(u) for u in unique]
        seen = set()
        for r in rows:
            key = tuple(r[i] for i in idx)
            if key in seen:
                problems.append(f"duplicate key {dict(zip(unique, key))}: {unique} must be unique")
                break
            seen.add(key)
    return problems
