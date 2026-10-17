"""Lint BigQuery SQL for the mistakes that cost money or give wrong answers. Static: nothing is run.

Rules (BQ001-BQ008). Errors fail the build; warnings are advice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .catalog import Catalog

DIRECTIVE = re.compile(r"--\s*bqci:\s*(.+)")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # error | warning
    message: str


def directives(sql: str) -> dict[str, str]:
    """`-- bqci: lookback_days=7` comments, as {key: value}."""
    out: dict[str, str] = {}
    for m in DIRECTIVE.finditer(sql):
        for pair in m.group(1).split():
            if "=" in pair:
                k, _, v = pair.partition("=")
                out[k] = v
    return out


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


def final_selects(tree: exp.Expression) -> list[exp.Select]:
    if isinstance(tree, exp.Select):
        return [tree]
    if isinstance(tree, exp.SetOperation):
        return final_selects(tree.left) + final_selects(tree.right)
    return []


def _is_star(e: exp.Expression) -> bool:
    return isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))


def real_tables(tree: exp.Expression) -> list[exp.Table]:
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    return [t for t in tree.find_all(exp.Table) if not (not t.db and not t.catalog and t.name in ctes)]


def fqn(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


def _filters_partition(select: exp.Select, column: str) -> bool:
    """Does this SELECT's WHERE (or a JOIN condition) mention the partition column?"""
    parts = []
    if select.args.get("where"):
        parts.append(select.args["where"])
    parts += [j.args["on"] for j in select.args.get("joins", []) if j.args.get("on")]
    return any(c.name == column for part in parts for c in part.find_all(exp.Column))


def lint(sql: str, catalog: Catalog) -> list[Finding]:
    tree = parse(sql)
    out: list[Finding] = []

    # BQ001: BigQuery bills for every column you read, so SELECT * on a wide table is the expensive habit.
    for sel in final_selects(tree):
        if any(_is_star(e) for e in sel.expressions):
            out.append(Finding("BQ001", "error", "SELECT * in the final query reads every column and is billed for all of them; list the columns"))

    # BQ002: a partitioned table read without a filter on its partition column scans every partition.
    for t in real_tables(tree):
        table = catalog.get(fqn(t))
        if table and table.partition_column:
            sel = t.find_ancestor(exp.Select)
            if sel is None or not _filters_partition(sel, table.partition_column):
                out.append(Finding("BQ002", "error", f"{fqn(t)} is partitioned on {table.partition_column} but this query does not filter on it, so it scans every partition"))

    # BQ003: results that depend on when the job ran cannot be reproduced or backfilled.
    for node in tree.find_all(exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentDatetime, exp.CurrentTime):
        out.append(Finding("BQ003", "error", f"{node.sql('bigquery')} makes the result depend on when it ran; take the date as a parameter (@run_date)"))

    # BQ004: a join with no condition is a cross join, however it is spelled.
    for j in tree.find_all(exp.Join):
        if not j.args.get("on") and not j.args.get("using"):
            out.append(Finding("BQ004", "error", "a join with no ON or USING condition is a cross join (rows multiply)"))

    # BQ005: NOT IN (subquery) returns nothing at all if the subquery contains a single NULL.
    for node in tree.find_all(exp.In):
        if node.args.get("query") is not None and isinstance(node.parent, exp.Not):
            out.append(Finding("BQ005", "error", "NOT IN (subquery) returns no rows if the subquery yields a NULL; use NOT EXISTS"))

    # BQ006: BigQuery raises an error on division by zero; SAFE_DIVIDE returns NULL instead.
    for _ in tree.find_all(exp.Div):
        out.append(Finding("BQ006", "warning", "use SAFE_DIVIDE: a / b fails the whole query when b is 0"))

    # BQ007: ORDER BY inside a subquery or CTE with no LIMIT is wasted work and does not order the final result.
    top = final_selects(tree)
    for sel in tree.find_all(exp.Select):
        if sel in top:
            continue
        if sel.args.get("order") and not sel.args.get("limit"):
            out.append(Finding("BQ007", "warning", "ORDER BY in a subquery or CTE without LIMIT does nothing useful"))

    # BQ008: every table must be project-qualified and known to the catalog, or it cannot be costed or tested.
    for t in real_tables(tree):
        if not (t.catalog and t.db):
            out.append(Finding("BQ008", "error", f"table {t.name!r} must be written as `project.dataset.table`"))
        elif catalog.get(fqn(t)) is None:
            out.append(Finding("BQ008", "error", f"table {fqn(t)} is not in tables.yaml, so its cost and schema are unknown"))
    return out
