from pathlib import Path

import pytest

from bqci import directives, lint, load_catalog
from bqci.contracts import check, load_contract
from bqci.cost import GIB, budget_problem, estimate
from bqci.duck import run_model

ROOT = Path(__file__).resolve().parent.parent
CAT = load_catalog()
MODELS = {p.stem: p.read_text(encoding="utf-8") for p in sorted((ROOT / "sql").glob("*.sql"))}


def rules(sql):
    return {f.rule for f in lint(sql, CAT)}


# --- the shipped models ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_shipped_model_lints_clean(name):
    assert lint(MODELS[name], CAT) == []


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_shipped_model_is_inside_its_own_cost_budget(name):
    assert budget_problem(MODELS[name], estimate(MODELS[name], CAT)) is None


# --- each lint rule fires, and stays quiet when it should -----------------------------------------------------------

ORDERS = "`acme-prod.sales.orders`"
FILT = "created_at >= TIMESTAMP '2026-10-01'"


@pytest.mark.parametrize(
    "rule,sql",
    [
        ("BQ001", "SELECT * FROM `acme-prod.sales.customers`"),
        ("BQ001", "SELECT c.* FROM `acme-prod.sales.customers` AS c"),
        ("BQ001", "SELECT customer_id FROM `acme-prod.sales.customers` UNION ALL SELECT * FROM `acme-prod.sales.customers`"),
        ("BQ002", f"SELECT order_id FROM {ORDERS}"),
        ("BQ002", f"SELECT order_id FROM {ORDERS} WHERE status = 'paid'"),
        ("BQ003", "SELECT CURRENT_DATE() AS d FROM `acme-prod.sales.customers`"),
        ("BQ003", f"SELECT order_id FROM {ORDERS} WHERE {FILT} AND created_at < CURRENT_TIMESTAMP()"),
        ("BQ004", "SELECT c.name FROM `acme-prod.sales.customers` AS c CROSS JOIN `acme-prod.sales.customers` AS d"),
        ("BQ004", "SELECT c.name FROM `acme-prod.sales.customers` AS c, `acme-prod.sales.customers` AS d"),
        ("BQ005", "SELECT name FROM `acme-prod.sales.customers` WHERE customer_id NOT IN (SELECT customer_id FROM `acme-prod.billing.invoices` WHERE issued_on > DATE '2026-01-01')"),
        ("BQ006", "SELECT amount / 2 AS h FROM `acme-prod.billing.payments` WHERE paid_on > DATE '2026-01-01'"),
        ("BQ007", "WITH s AS (SELECT customer_id FROM `acme-prod.sales.customers` ORDER BY customer_id) SELECT customer_id FROM s"),
        ("BQ008", "SELECT 1 FROM `acme-prod.sales.nope`"),
        ("BQ008", "SELECT name FROM customers"),
    ],
)
def test_each_rule_fires_on_the_mistake_it_names(rule, sql):
    assert rule in rules(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS n FROM `acme-prod.sales.customers`",  # COUNT(*) is not SELECT *
        f"SELECT order_id FROM {ORDERS} WHERE {FILT}",  # filtered on the partition column
        f"SELECT o.order_id FROM {ORDERS} AS o JOIN `acme-prod.sales.customers` AS c ON c.customer_id = o.customer_id WHERE o.{FILT}",
        "SELECT name FROM `acme-prod.sales.customers` AS c WHERE NOT EXISTS (SELECT 1 FROM `acme-prod.billing.invoices` AS i WHERE i.customer_id = c.customer_id AND i.issued_on > DATE '2026-01-01')",
        "SELECT SAFE_DIVIDE(amount, 2) AS h FROM `acme-prod.billing.payments` WHERE paid_on > DATE '2026-01-01'",
        "WITH s AS (SELECT customer_id FROM `acme-prod.sales.customers` ORDER BY customer_id LIMIT 5) SELECT customer_id FROM s",
        "SELECT customer_id, ROW_NUMBER() OVER (ORDER BY customer_id) AS rn FROM `acme-prod.sales.customers`",  # ORDER BY in a window is fine
        "WITH s AS (SELECT customer_id FROM `acme-prod.sales.customers`) SELECT customer_id FROM s",  # a CTE name is not an unknown table
    ],
)
def test_the_rules_stay_quiet_on_correct_sql(sql):
    assert rules(sql) == set()


def test_a_partition_filter_in_a_join_condition_counts():
    sql = (f"SELECT c.name FROM `acme-prod.sales.customers` AS c LEFT JOIN {ORDERS} AS o "
           f"ON o.customer_id = c.customer_id AND o.{FILT}")
    assert "BQ002" not in rules(sql)


def test_a_filter_on_a_different_column_does_not_satisfy_the_partition_rule():
    assert "BQ002" in rules(f"SELECT order_id FROM {ORDERS} WHERE order_id = 'x'")


def test_the_partition_rule_looks_at_each_select_not_just_the_outer_one():
    sql = f"SELECT order_id FROM (SELECT order_id FROM {ORDERS}) AS t WHERE order_id = 'x'"
    assert "BQ002" in rules(sql)


def test_severities_are_errors_for_cost_and_correctness_and_warnings_for_style():
    sev = {f.rule: f.severity for f in lint("SELECT * FROM `acme-prod.sales.customers` WHERE 1 / 1 = 1", CAT)}
    assert sev["BQ001"] == "error" and sev["BQ006"] == "warning"


def test_directives_are_parsed_from_the_header_comment():
    assert directives("-- bqci: lookback_days=7 max_gib=2\nSELECT 1") == {"lookback_days": "7", "max_gib": "2"}
    assert directives("SELECT 1") == {}


# --- cost ---------------------------------------------------------------------------------------------------------------


def test_cost_counts_only_the_columns_the_query_reads():
    narrow = estimate(f"SELECT order_id FROM {ORDERS} WHERE {FILT}", CAT)
    wide = estimate(f"SELECT * FROM {ORDERS} WHERE {FILT}", CAT)
    cols = narrow.tables[0].columns
    assert cols == ["created_at", "order_id"], "the WHERE column is read too"
    assert wide.full_bytes > 15 * narrow.full_bytes, "SELECT * reads notes and shipping_address as well"


def test_cost_matches_the_hand_calculation_for_daily_revenue():
    e = estimate(MODELS["daily_revenue"], CAT)
    # created_at 8 + currency 5 + amount 16 + status 10 = 39 bytes per row, over 2 billion rows
    assert e.full_bytes == 39 * 2_000_000_000
    assert e.scanned_bytes == int(39 * 2_000_000_000 * 7 / 730)  # 7 of 730 daily partitions
    assert e.tables[0].pruned is True


def test_pruning_is_only_applied_when_the_query_really_filters_on_the_partition_column():
    declared_but_unfiltered = "-- bqci: lookback_days=7\nSELECT order_id FROM `acme-prod.sales.orders`"
    e = estimate(declared_but_unfiltered, CAT)
    assert e.scanned_bytes == e.full_bytes and e.tables[0].pruned is False


def test_a_lookback_covering_the_whole_table_prunes_nothing():
    e = estimate(MODELS["customer_ltv"], CAT)
    orders = next(t for t in e.tables if t.table.endswith(".orders"))
    assert orders.scanned_bytes == orders.full_bytes and orders.pruned is False


def test_a_join_to_a_big_table_is_costed_per_table():
    e = estimate(MODELS["late_payments"], CAT)
    assert {t.table for t in e.tables} == {"acme-prod.billing.invoices", "acme-prod.billing.payments"}
    assert all(t.pruned for t in e.tables)


def test_the_budget_check_catches_an_overspend_and_a_missing_budget():
    sql = MODELS["customer_ltv"].replace("max_gib=140", "max_gib=10")
    assert "over the 10 GiB budget" in budget_problem(sql, estimate(sql, CAT))
    nobudget = MODELS["daily_revenue"].replace(" max_gib=2", "")
    assert "no `max_gib`" in budget_problem(nobudget, estimate(nobudget, CAT))


def test_an_unknown_table_cannot_be_costed():
    with pytest.raises(Exception):
        estimate("SELECT 1 FROM `acme-prod.sales.nope`", CAT)


# --- contracts -----------------------------------------------------------------------------------------------------------

GOOD_ROW = {"sales.orders": [{"order_id": "a", "customer_id": "c", "created_at": "2026-10-16 01:00:00", "status": "paid",
                              "currency": "USD", "amount": 5}]}
RUN = {"run_date": "2026-10-16"}


def run(sql, given=GOOD_ROW):
    return run_model(sql, CAT, given, RUN)


def test_a_renamed_column_breaks_the_contract():
    sql = MODELS["daily_revenue"].replace("AS revenue", "AS total_revenue")
    problems = check(load_contract("daily_revenue"), *run(sql))
    assert problems and "columns are" in problems[0]


def test_a_changed_column_type_breaks_the_contract():
    sql = MODELS["daily_revenue"].replace("COUNT(*) AS orders", "CAST(COUNT(*) AS STRING) AS orders")
    assert any("orders is VARCHAR" in p for p in check(load_contract("daily_revenue"), *run(sql)))


def test_a_null_in_a_not_null_column_breaks_the_contract():
    sql = MODELS["daily_revenue"].replace("o.currency,\n  COUNT", "NULLIF(o.currency, 'USD') AS currency,\n  COUNT").replace("GROUP BY day, o.currency", "GROUP BY day, 2")
    assert any("currency is declared not-null" in p for p in check(load_contract("daily_revenue"), *run(sql)))


def test_a_fan_out_that_duplicates_the_key_breaks_the_contract():
    """Group by one column MORE (the order id) and two USD orders on one day become two rows with the same (day, currency)."""
    given = {"sales.orders": [
        {"order_id": "a", "customer_id": "c", "created_at": "2026-10-16 01:00:00", "status": "paid", "currency": "USD", "amount": 1},
        {"order_id": "b", "customer_id": "c", "created_at": "2026-10-16 02:00:00", "status": "paid", "currency": "USD", "amount": 2}]}
    sql = MODELS["daily_revenue"].replace("GROUP BY day, o.currency", "GROUP BY day, o.currency, o.order_id")
    problems = check(load_contract("daily_revenue"), *run(sql, given))
    assert any("duplicate key" in p for p in problems)


def test_numeric_may_come_back_as_double_from_duckdb_and_that_is_a_documented_blind_spot():
    cols, types, rows = run_model(MODELS["customer_ltv"], CAT, {"sales.customers": [{"customer_id": "c", "name": "n"}]}, RUN)
    assert types[cols.index("avg_order_value")] == "DOUBLE"
    assert check(load_contract("customer_ltv"), cols, types, rows) == []
