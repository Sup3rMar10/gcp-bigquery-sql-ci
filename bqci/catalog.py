"""The table catalog (tables.yaml) as Python objects, and a few BigQuery type facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DUCKDB_TYPES = {
    "STRING": "VARCHAR", "INT64": "BIGINT", "NUMERIC": "DECIMAL(38,9)", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN",
    "DATE": "DATE",
    # BigQuery's TIMESTAMP is an absolute instant (UTC), which is DuckDB's TIMESTAMPTZ, not its naive TIMESTAMP.
    "TIMESTAMP": "TIMESTAMPTZ",
}


@dataclass
class Table:
    name: str  # project.dataset.table
    rows: int
    columns: dict[str, dict]
    partition_column: str | None = None
    partition_days: int | None = None

    @property
    def dataset_table(self) -> tuple[str, str]:
        _, dataset, table = self.name.split(".")
        return dataset, table

    def column_bytes(self, col: str) -> int:
        return int(self.columns[col]["bytes"])

    @property
    def row_bytes(self) -> int:
        return sum(self.column_bytes(c) for c in self.columns)


@dataclass
class Catalog:
    tables: dict[str, Table] = field(default_factory=dict)

    def get(self, fqn: str) -> Table | None:
        return self.tables.get(fqn)

    def sqlglot_schema(self) -> dict:
        """{project: {dataset: {table: {column: type}}}} for sqlglot's column qualification."""
        out: dict = {}
        for t in self.tables.values():
            project, dataset, table = t.name.split(".")
            out.setdefault(project, {}).setdefault(dataset, {})[table] = {c: spec["type"] for c, spec in t.columns.items()}
        return out


def load_catalog(path: str | Path = ROOT / "tables.yaml") -> Catalog:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["tables"]
    tables = {}
    for name, spec in raw.items():
        part = spec.get("partition") or {}
        tables[name] = Table(name, int(spec["rows"]), spec["columns"], part.get("column"), part.get("days"))
        if part.get("column") and part["column"] not in spec["columns"]:
            raise ValueError(f"{name}: the partition column {part['column']!r} is not a column")
    return Catalog(tables)
