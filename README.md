# gcp-bigquery-sql-ci

![ci](https://github.com/Sup3rMar10/gcp-bigquery-sql-ci/actions/workflows/ci.yml/badge.svg)

CI for **BigQuery SQL** that runs without a BigQuery project: it **lints** the SQL for mistakes that cost money or return wrong answers, **estimates scan cost** from the columns a query reads, **runs every model over fixture data** (in DuckDB) to unit-test its logic, and checks the output against a **data contract**.

> **What is verified, and what is not.** 91 tests pass locally on Python 3.13, the three `bqci` commands pass, and CI repeats this on Linux. I changed the models on purpose four times (a window boundary, a `<=` for `<`, the wrong status excluded, a dropped `IFNULL`) and tests failed each time. **Nothing has run on BigQuery.** The cost figures are a *model* of BigQuery's billing, and the unit tests run in DuckDB, which is not BigQuery (see the limits, including a real divergence I hit). The `dry-run.yml` workflow, which asks BigQuery itself, was never run.

## What it checks

| Check | Command | What it catches |
|---|---|---|
| **Lint** (8 rules, `sqlglot` parsing BigQuery SQL) | `python -m bqci lint` | `BQ001` `SELECT *` (BigQuery bills for every column), `BQ002` a partitioned table read with no filter on its partition column, `BQ003` `CURRENT_DATE`/`NOW` (results that depend on when it ran), `BQ004` a join with no condition, `BQ005` `NOT IN (subquery)` (returns nothing if the subquery has one NULL), `BQ006` `/` instead of `SAFE_DIVIDE` (warning), `BQ007` `ORDER BY` in a subquery with no `LIMIT` (warning), `BQ008` a table that is not project-qualified or not in the catalog |
| **Cost model** | `python -m bqci cost` | estimated bytes scanned per model against its own `max_gib` budget |
| **Logic tests** | `python -m bqci test` | each model over 20 fixture cases (boundaries, NULLs, empty inputs, fan-out) |
| **Data contract** | part of the above | output column names and order, type family, not-null, and uniqueness of the declared key |

## The models (`sql/`)

`daily_revenue` (7 days ending on `@run_date`), `customer_ltv` (two years, LEFT JOIN so customers with no orders still appear), `late_payments` (invoices more than 30 days overdue and not fully paid). Dates come from a `@run_date` parameter, never from the clock, so a run can be repeated and backfilled.

## The cost model, in one example

BigQuery on-demand billing counts **only the columns a query reads**, and a filter on the partition column lets it skip partitions. `bqci cost` reproduces both from the catalog (`tables.yaml`, invented sizes):

```
model              scanned GiB  unpruned GiB  budget
customer_ltv            132.62         132.6  within budget
daily_revenue             0.70          72.6  within budget
late_payments             3.28          30.0  within budget
```

`daily_revenue` reads 4 of 8 columns of a 2-billion-row table, and 7 of 730 daily partitions, so about 0.7 GiB instead of 72.6. The same query written `SELECT *` costs about 23 times a single-column one in this model, because it also reads the wide `notes` and `shipping_address` columns. Partition pruning is credited only if the query **really filters** on the partition column (a `lookback_days` header with no filter earns nothing; tested).

## What the tests cover

- **Boundaries**, which is where date-window SQL is usually wrong: the 7-day window is inclusive at both ends (a row at `23:59:59` the day before is out, one at `23:59:59` on `run_date` is in, midnight after is out); "more than 30 days overdue" means strictly more (30 is out, 31 is in); the two-year window includes its first instant and excludes the second before it; a payment dated after `run_date` has not happened yet.
- **NULLs and empties**: a NULL amount still counts as an order but adds nothing; a group whose amounts are all NULL gives NULL revenue, not zero; no input gives no rows; a customer with only cancelled orders looks like one with none; orders of an unknown customer create no row.
- **The contract can fail**: a renamed column, a changed type, a NULL in a not-null column and a fan-out that duplicates the key are each caught (and each shipped case is also checked against the contract).
- Each lint rule fires on the mistake it names *and* stays quiet on correct SQL (a CTE name is not an unknown table; `COUNT(*)` is not `SELECT *`; `ORDER BY` inside a window is fine).

## Things I got wrong or hit

- **A real BigQuery/DuckDB divergence:** BigQuery's `SAFE_DIVIDE(NUMERIC, INT64)` is `NUMERIC`; DuckDB computes it as `DOUBLE`. The contract rightly says `NUMERIC`, so my first contract test failed. The DuckDB-side check now accepts either numeric family for `NUMERIC`, and a test records that it **cannot tell a computed `NUMERIC` from a `FLOAT64`**. Only a dry run or real run can.
- BigQuery's `TIMESTAMP` is an absolute instant, so it maps to DuckDB's `TIMESTAMPTZ` with the session pinned to UTC; mapping it to a naive `TIMESTAMP` would compare wrongly.
- **CI failed on its first run**: DuckDB needs `pytz` to return `TIMESTAMPTZ` values to Python, and it happened to be installed on my machine. Both Linux jobs failed on every model that outputs a timestamp until I added a `requirements.txt`.
- One of my own contract tests was wrong (a "fan-out" with two different currencies still has unique keys); the test now uses two orders in the same currency.

## Honest limits

- **DuckDB is not BigQuery.** `sqlglot` translates BigQuery functions, but type coercion, rounding, NULL ordering, timezone handling and function edge cases can differ, and anything BigQuery-specific that DuckDB cannot express (some `ARRAY`/`STRUCT` and geography functions, scripting) cannot be tested this way. A passing test here is evidence about the model's logic, not proof it behaves identically in BigQuery.
- **The cost figure is a model.** It ignores clustering, caching, the minimum billed bytes per table, materialised views and the real planner; row counts and column widths are invented numbers in `tables.yaml` (a real catalog would come from `INFORMATION_SCHEMA`). `lookback_days` is declared by the author and only checked to the extent that the query filters on the partition column at all, not that the filter really spans that many days. `bq query --dry_run` is the authority.
- **The partition rule checks that a filter exists, not that it prunes.** A filter such as `WHERE created_at IS NOT NULL` mentions the column and satisfies it, though it prunes nothing.
- The lint is syntactic: `BQ005` and `BQ004` find the patterns, not every way to write a bug, and nothing here checks whether a join key is unique (the classic fan-out), beyond what a fixture happens to exercise.
- Fixtures are small hand-written tables; they show the logic on the cases someone thought of, not on production data volumes or skew.
- `dry-run.yml` is a reference workflow, never run.

## Run it

```
python -m pip install -r requirements.txt
python -m pytest
python -m bqci lint
python -m bqci cost
python -m bqci test
```

To add a model: put `sql/NAME.sql` (with a `-- bqci: lookback_days=N max_gib=M` header), a `contracts/NAME.yaml`, and cases in `tests/cases/NAME.yaml`; a test fails if any of the three is missing.
