"""The gate against a real Postgres: each test plants one problem and checks the rule that must catch it."""

from __future__ import annotations

import csv
import json
from datetime import timedelta

import psycopg
import pytest

from data_quality.gate import GateConfig, GateResult, Outcome, run_gate
from data_quality.report import write_artifacts
from data_quality.rules import RULES
from tests.data_quality.seed import AS_OF, ORDERS_PER_DAY

pytestmark = pytest.mark.integration

CONFIG = GateConfig(as_of=AS_OF, lookback_days=28)


def run(conn: psycopg.Connection) -> GateResult:
    result = run_gate(conn, CONFIG)
    conn.commit()
    return result


def by_id(gate: GateResult) -> dict:
    return {r.rule.rule_id: r for r in gate.results}


def quarantined_keys(conn: psycopg.Connection, rule_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT row_key FROM dq_quarantine WHERE run_id = %s AND rule_id = %s ORDER BY row_key::text",
        (CONFIG.run_id, rule_id),
    ).fetchall()
    return [r[0] for r in rows]


def add_order(conn, order_id, customer_id, day, total=20.00, status="completed", with_item=True):
    conn.execute(
        "INSERT INTO orders VALUES (%s, %s, %s, %s, %s)",
        (order_id, customer_id, day, status, total),
    )
    if with_item:
        conn.execute("INSERT INTO order_items VALUES (%s, %s, 1, 2, 10.00)", (order_id, order_id))


def test_clean_data_passes_every_rule(seeded):
    gate = run(seeded)
    assert gate.passed
    failing = {r.rule.rule_id: r.violations for r in gate.results if r.outcome is not Outcome.PASS}
    assert failing == {}
    assert sum(r.violations for r in gate.results) == 0
    assert len(gate.results) == len(RULES)


def test_few_undated_orders_are_quarantined_without_blocking(seeded):
    add_order(seeded, 9001, 1, None)
    gate = run(seeded)
    rule = by_id(gate)["ORD_ORDER_DATE_NOT_NULL"]
    assert (rule.violations, rule.quarantined, rule.outcome) == (1, 1, Outcome.PASS)
    assert quarantined_keys(seeded, "ORD_ORDER_DATE_NOT_NULL") == [{"order_id": 9001}]
    assert gate.passed


def test_many_undated_orders_block_the_gate(seeded):
    seeded.execute("UPDATE orders SET order_date = NULL WHERE order_id % 5 = 0")  # 20% > 5% tolerance
    gate = run(seeded)
    assert by_id(gate)["ORD_ORDER_DATE_NOT_NULL"].outcome is Outcome.BLOCK
    assert not gate.passed


def test_future_dated_events_are_quarantined(seeded):
    seeded.execute(
        "INSERT INTO events VALUES (99001, 1, %s, 'view', 1, 'future')", (AS_OF + timedelta(days=300),)
    )
    rule = by_id(run(seeded))["EVT_NOT_FUTURE"]
    assert (rule.violations, rule.quarantined) == (1, 1)
    assert quarantined_keys(seeded, "EVT_NOT_FUTURE")[0]["event_id"] == 99001


def test_order_total_mismatch_is_caught(seeded):
    seeded.execute("UPDATE orders SET order_total = 25.00 WHERE order_id = 10")
    rule = by_id(run(seeded))["ORD_TOTAL_MATCHES_ITEMS"]
    assert (rule.violations, rule.quarantined) == (1, 1)
    assert quarantined_keys(seeded, "ORD_TOTAL_MATCHES_ITEMS") == [{"order_id": 10}]


def test_invalid_status_spelling_is_caught(seeded):
    seeded.execute("UPDATE orders SET status = 'Completed' WHERE order_id IN (1, 2)")
    rule = by_id(run(seeded))["ORD_STATUS_VALID"]
    assert rule.violations == 2
    assert rule.outcome is Outcome.PASS  # 2 of 210 orders is within the 1% tolerance


def test_orphan_event_customer_is_caught(seeded):
    seeded.execute(
        "INSERT INTO events VALUES (99002, 777, %s, 'view', 1, 'orphan')", (AS_OF - timedelta(days=1),)
    )
    assert by_id(run(seeded))["EVT_CUSTOMER_EXISTS"].violations == 1


def test_order_without_items_is_caught(seeded):
    add_order(seeded, 9002, 2, AS_OF - timedelta(days=40), with_item=False)
    run(seeded)
    assert quarantined_keys(seeded, "ORD_HAS_ITEMS") == [{"order_id": 9002}]


def test_exact_duplicate_event_and_id_collision(seeded):
    seeded.execute("INSERT INTO events SELECT * FROM events WHERE event_id = 1")  # exact copy
    seeded.execute(
        "INSERT INTO events VALUES (2, 4, %s, 'purchase', 2, 'other')", (AS_OF - timedelta(days=3),)
    )  # same event_id, different event
    rules = by_id(run(seeded))
    assert rules["EVT_DUPLICATE_ROWS"].violations == 1
    assert rules["EVT_EVENT_ID_UNIQUE"].violations == 2


def test_high_volume_customer_is_excluded(seeded):
    day = AS_OF - timedelta(days=200)  # outside the volume-rule window
    for i in range(731):
        add_order(seeded, 50_000 + i, 4, day)
    gate = run(seeded)
    rule = by_id(gate)["CUS_ORDER_VOLUME_PLAUSIBLE"]
    assert (rule.violations, rule.quarantined) == (1, 1)
    excluded = seeded.execute(
        "SELECT customer_id, orders_in_window, rule_id FROM dq_excluded_customers"
    ).fetchall()
    assert excluded == [(4, 731, "CUS_ORDER_VOLUME_PLAUSIBLE")]


def test_missing_day_of_orders_is_a_blocking_volume_drop(seeded):
    missing_day = AS_OF - timedelta(days=3)
    seeded.execute(
        "DELETE FROM order_items WHERE order_id IN (SELECT order_id FROM orders WHERE order_date::date = %s)",
        (missing_day,),
    )
    seeded.execute("DELETE FROM orders WHERE order_date::date = %s", (missing_day,))
    gate = run(seeded)
    rule = by_id(gate)["ORD_DAILY_VOLUME_DROP"]
    assert rule.outcome is Outcome.BLOCK
    assert rule.sample[0]["day"] == missing_day
    assert rule.sample[0]["n"] == 0
    assert not gate.passed


def test_volume_spike_only_warns(seeded):
    day = AS_OF - timedelta(days=2)
    for i in range(4 * ORDERS_PER_DAY):
        add_order(seeded, 60_000 + i, 1 + i % 3, day)
    gate = run(seeded)
    assert by_id(gate)["ORD_DAILY_VOLUME_SPIKE"].outcome is Outcome.WARN
    assert gate.passed


def test_stale_orders_block_on_freshness(seeded):
    seeded.execute(
        "UPDATE orders SET order_date = order_date - INTERVAL '30 days' WHERE order_date >= %s",
        (AS_OF - timedelta(days=5),),
    )
    assert by_id(run(seeded))["ORD_FRESHNESS"].outcome is Outcome.BLOCK


def test_rerun_is_idempotent(seeded):
    add_order(seeded, 9003, 3, None)
    run(seeded)
    run(seeded)
    assert quarantined_keys(seeded, "ORD_ORDER_DATE_NOT_NULL") == [{"order_id": 9003}]
    (results,) = seeded.execute(
        "SELECT COUNT(*) FROM dq_rule_results WHERE run_id = %s", (CONFIG.run_id,)
    ).fetchone()
    assert results == len(RULES)


def test_artifacts_contain_report_and_quarantine_sample(seeded, tmp_path):
    add_order(seeded, 9004, 1, None)
    gate = run(seeded)
    run_dir = write_artifacts(gate, tmp_path, seeded)
    report = json.loads((run_dir / "dq_report.json").read_text())
    assert report["gate_status"] == "PASS"
    assert report["summary"]["rows_quarantined"] == 1
    with (run_dir / "quarantine_sample.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["rule_id"], json.loads(r["row_key"])) for r in rows] == [
        ("ORD_ORDER_DATE_NOT_NULL", {"order_id": 9004})
    ]
