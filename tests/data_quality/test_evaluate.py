"""Outcome logic: tolerance, severity and edge cases, independent of any database."""

import pytest

from data_quality.gate import GateConfig, Outcome, evaluate
from data_quality.rules import Dimension, Rule, Severity


def make_rule(severity: Severity = Severity.BLOCK, max_failure_rate: float = 0.0) -> Rule:
    return Rule(
        rule_id="TST_RULE",
        dimension=Dimension.VALIDITY,
        table="orders",
        description="test rule",
        severity=severity,
        checked_sql="SELECT 1",
        violations_sql="SELECT 1 AS k WHERE FALSE",
        key_columns=("k",),
        max_failure_rate=max_failure_rate,
    )


def test_no_violations_passes():
    assert evaluate(make_rule(), rows_checked=100, violations=0) == (0.0, Outcome.PASS)


def test_any_violation_fails_a_zero_tolerance_rule():
    rate, outcome = evaluate(make_rule(Severity.BLOCK), rows_checked=1000, violations=1)
    assert rate == pytest.approx(0.001)
    assert outcome is Outcome.BLOCK


def test_warn_severity_warns_instead_of_blocking():
    _, outcome = evaluate(make_rule(Severity.WARN), rows_checked=10, violations=5)
    assert outcome is Outcome.WARN


def test_violations_within_tolerance_pass():
    # 2% undated orders against a 5% tolerance: rows are quarantined but the gate does not block.
    rate, outcome = evaluate(make_rule(max_failure_rate=0.05), rows_checked=500_000, violations=10_016)
    assert rate == pytest.approx(0.020032)
    assert outcome is Outcome.PASS


def test_rate_exactly_at_tolerance_passes_and_just_above_fails():
    rule = make_rule(max_failure_rate=0.01)
    assert evaluate(rule, rows_checked=100, violations=1)[1] is Outcome.PASS
    assert evaluate(rule, rows_checked=100, violations=2)[1] is Outcome.BLOCK


def test_nothing_checked_with_violations_counts_as_full_failure():
    assert evaluate(make_rule(), rows_checked=0, violations=1) == (1.0, Outcome.BLOCK)


def test_nothing_checked_and_no_violations_passes():
    assert evaluate(make_rule(), rows_checked=0, violations=0) == (0.0, Outcome.PASS)


def test_negative_counts_are_rejected():
    with pytest.raises(ValueError):
        evaluate(make_rule(), rows_checked=-1, violations=0)


def test_run_id_is_deterministic_per_date():
    from datetime import date

    assert GateConfig(as_of=date(2026, 7, 1)).run_id == "dq_20260701"
    assert (
        GateConfig(as_of=date(2026, 7, 1)).run_id
        == GateConfig(as_of=date(2026, 7, 1), lookback_days=7).run_id
    )


def test_lookback_must_be_positive():
    from datetime import date

    with pytest.raises(ValueError):
        GateConfig(as_of=date(2026, 7, 1), lookback_days=0)
