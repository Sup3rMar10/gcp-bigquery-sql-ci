"""Run a BigQuery model against small fixture tables in DuckDB, so its logic is unit-tested without BigQuery.

The SQL is parsed as BigQuery, its project-qualified tables are rewritten to dataset.table, its @parameters are replaced
with literals, and it is transpiled to DuckDB (sqlglot translates BigQuery functions such as COUNTIF, SAFE_DIVIDE, DATE_SUB
and DATE_DIFF). Timestamps are TIMESTAMPTZ and the session is UTC, because a BigQuery TIMESTAMP is an absolute instant.

This tests the model's LOGIC. DuckDB is not BigQuery: type coercion, NULL ordering, rounding and function edge cases can
differ, so a passing test here is evidence, not proof, and a dry run on real BigQuery is still the final check.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import duckdb
from sqlglot import exp

from .catalog import DUCKDB_TYPES, Catalog
from .lint import parse


def _literal(value) -> exp.Expression:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        return exp.cast(exp.Literal.string(value), "DATE")
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def to_duckdb_sql(sql: str, params: dict) -> str:
    tree = parse(sql)
    for p in list(tree.find_all(exp.Parameter)):
        name = p.this.name if hasattr(p.this, "name") else str(p.this)
        if name not in params:
            raise KeyError(f"the model uses @{name} but no value was given")
        p.replace(_literal(params[name]))
    for t in tree.find_all(exp.Table):
        if t.catalog:
            t.set("catalog", None)  # `acme-prod.sales.orders` -> sales.orders
    return tree.sql(dialect="duckdb")


def _normalise(value):
    if isinstance(value, Decimal):
        return float(round(value, 6))
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if value.tzinfo else value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def run_model(sql: str, catalog: Catalog, given: dict[str, list[dict]], params: dict):
    """Return (column names, DuckDB type names, rows as lists) for the model run over the given table contents.

    `given` maps `dataset.table` to a list of row dicts; every catalog table exists, empty unless given rows.
    """
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    for table in catalog.tables.values():
        dataset, name = table.dataset_table
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{dataset}"')
        cols = ", ".join(f'"{c}" {DUCKDB_TYPES[spec["type"]]}' for c, spec in table.columns.items())
        con.execute(f'CREATE TABLE "{dataset}"."{name}" ({cols})')
    for key, rows in given.items():
        dataset, name = key.split(".")
        for row in rows:
            names = ", ".join(f'"{c}"' for c in row)
            con.execute(f'INSERT INTO "{dataset}"."{name}" ({names}) VALUES ({", ".join("?" for _ in row)})', list(row.values()))
    cur = con.execute(to_duckdb_sql(sql, params))
    cols = [d[0] for d in cur.description]
    types = [str(d[1]) for d in cur.description]
    rows = [[_normalise(v) for v in r] for r in cur.fetchall()]
    return cols, types, rows


def as_dicts(cols: list[str], rows: list[list]) -> list[dict]:
    return [dict(zip(cols, r)) for r in rows]
