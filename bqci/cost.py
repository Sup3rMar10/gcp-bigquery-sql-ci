"""Estimate how many bytes a query would scan, from the SQL and the catalog alone. A MODEL of BigQuery's billing, not a dry run.

What it captures (all true of BigQuery on-demand queries):
  - billing is by the columns a query reads: only the columns it actually references count, which is why SELECT * on a wide
    table is expensive and a narrow column list is cheap
  - a filter on a partition column lets BigQuery skip partitions; the query declares how many days it reads with
    `-- bqci: lookback_days=N`, and the estimate prunes only when the query really does filter on the partition column
What it does NOT capture: clustering, caching, the minimum billed bytes per table, materialised views, or BigQuery's real
planner. `bq query --dry_run` is the authority; the workflow has a gated job for it, never run here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from .catalog import Catalog
from .lint import _filters_partition, directives, fqn, parse

GIB = 1024 ** 3


@dataclass
class TableCost:
    table: str
    columns: list[str]
    full_bytes: int  # every partition
    scanned_bytes: int  # after partition pruning, if the query earns it
    pruned: bool


@dataclass
class Estimate:
    tables: list[TableCost]

    @property
    def scanned_bytes(self) -> int:
        return sum(t.scanned_bytes for t in self.tables)

    @property
    def full_bytes(self) -> int:
        return sum(t.full_bytes for t in self.tables)


def referenced_columns(sql: str, catalog: Catalog) -> dict[str, set[str]]:
    """Real table -> the columns the query actually references. `SELECT *` is expanded to every column first."""
    tree = qualify(parse(sql), schema=catalog.sqlglot_schema(), dialect="bigquery")
    used: dict[str, set[str]] = defaultdict(set)
    for scope in traverse_scope(tree):
        for col in scope.columns:
            source = scope.sources.get(col.table)
            if isinstance(source, exp.Table):
                used[fqn(source)].add(col.name)
        for source in scope.sources.values():  # a table that is read but whose columns are never named still appears
            if isinstance(source, exp.Table):
                used.setdefault(fqn(source), set())
    return used


def _every_use_is_filtered(sql: str, table_fqn: str, partition_column: str) -> bool:
    uses = [t for t in parse(sql).find_all(exp.Table) if fqn(t) == table_fqn]
    for t in uses:
        sel = t.find_ancestor(exp.Select)
        if sel is None or not _filters_partition(sel, partition_column):
            return False
    return bool(uses)


def estimate(sql: str, catalog: Catalog) -> Estimate:
    lookback = directives(sql).get("lookback_days")
    out = []
    for name, cols in sorted(referenced_columns(sql, catalog).items()):
        table = catalog.get(name)
        if table is None:
            raise KeyError(f"{name} is not in the catalog")
        full = table.rows * sum(table.column_bytes(c) for c in cols)
        scanned, pruned = full, False
        if table.partition_column and lookback and _every_use_is_filtered(sql, name, table.partition_column):
            scanned = int(full * min(1.0, int(lookback) / table.partition_days))
            pruned = scanned < full
        out.append(TableCost(name, sorted(cols), full, scanned, pruned))
    return Estimate(out)


def budget_problem(sql: str, est: Estimate) -> str | None:
    """A message if the estimate exceeds the model's own `max_gib` budget, else None."""
    limit = directives(sql).get("max_gib")
    if limit is None:
        return "the model declares no `max_gib` budget"
    if est.scanned_bytes > float(limit) * GIB:
        return f"estimated {est.scanned_bytes / GIB:.2f} GiB scanned, over the {limit} GiB budget"
    return None
