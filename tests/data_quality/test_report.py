"""Report content and the command line, without a database."""

import json
from datetime import UTC, date, datetime

import pytest

import data_quality.__main__ as cli
from data_quality.gate import GateConfig, GateResult, Outcome, RuleResult
from data_quality.report import build_report, render_markdown, write_artifacts
from data_quality.rules import Dimension, Rule, Severity


def make_result(rule_id: str, outcome: Outcome, dimension=Dimension.VALIDITY, violations=0, quarantined=0):
    rule = Rule(
        rule_id=rule_id,
        dimension=dimension,
        table="orders",
        description=f"{rule_id} description",
        severity=Severity.BLOCK if outcome is Outcome.BLOCK else Severity.WARN,
        checked_sql="SELECT 1",
        violations_sql="SELECT 1 AS k",
        key_columns=("k",),
    )
    return RuleResult(
        rule=rule,
        rows_checked=100,
        violations=violations,
        failure_rate=violations / 100,
        outcome=outcome,
        quarantined=quarantined,
        sample=[{"k": 1, "order_date": date(2026, 6, 30)}] if violations else [],
    )


@pytest.fixture
def gate_result() -> GateResult:
    return GateResult(
        config=GateConfig(as_of=date(2026, 7, 1)),
        results=[
            make_result("ORD_OK", Outcome.PASS),
            make_result("ORD_WARN", Outcome.WARN, Dimension.CONSISTENCY, violations=5),
            make_result("ORD_BLOCK", Outcome.BLOCK, Dimension.CONSISTENCY, violations=7, quarantined=7),
        ],
        seconds=1.23,
    )


def test_report_summarises_outcomes_and_dimensions(gate_result):
    report = build_report(gate_result, generated_at=datetime(2026, 7, 1, tzinfo=UTC))
    assert report["gate_status"] == "FAIL"
    assert report["run_id"] == "dq_20260701"
    summary = report["summary"]
    assert (summary["passed"], summary["warnings"], summary["blocking"]) == (1, 1, 1)
    assert summary["rows_quarantined"] == 7
    assert summary["blocking_rules"] == ["ORD_BLOCK"]
    assert summary["by_dimension"] == {
        "validity": {"rules": 1, "failed": 0},
        "consistency": {"rules": 2, "failed": 2},
    }
    assert [r["rule_id"] for r in report["rules"]] == ["ORD_OK", "ORD_WARN", "ORD_BLOCK"]


def test_report_without_blocking_rules_passes(gate_result):
    passing = GateResult(gate_result.config, gate_result.results[:2], gate_result.seconds)
    assert build_report(passing)["gate_status"] == "PASS"


def test_markdown_lists_every_rule_and_the_status(gate_result):
    markdown = render_markdown(build_report(gate_result))
    assert "Gate status: FAIL" in markdown
    assert "blocking rules: ORD_BLOCK" in markdown
    for rule_id in ("ORD_OK", "ORD_WARN", "ORD_BLOCK"):
        assert f"`{rule_id}`" in markdown


def test_artifacts_are_written_per_run_and_json_handles_dates(gate_result, tmp_path):
    run_dir = write_artifacts(gate_result, tmp_path)
    assert run_dir == tmp_path / "dq_20260701"
    report = json.loads((run_dir / "dq_report.json").read_text())
    assert report["rules"][2]["sample"][0]["order_date"] == "2026-06-30"
    assert (run_dir / "dq_report.md").read_text().startswith("# Data quality report")


def test_cli_requires_as_of():
    with pytest.raises(SystemExit):
        cli.parse_args([])


def test_cli_without_database_url_is_a_config_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["--as-of", "2026-07-01"]) == cli.EXIT_CONFIG_ERROR


def test_cli_rejects_invalid_lookback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    assert cli.main(["--as-of", "2026-07-01", "--lookback-days", "0"]) == cli.EXIT_CONFIG_ERROR


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(("blocked", "expected"), [(False, cli.EXIT_PASS), (True, cli.EXIT_BLOCKED)])
def test_cli_exit_code_follows_gate_status(monkeypatch, tmp_path, gate_result, blocked, expected):
    results = gate_result.results if blocked else gate_result.results[:2]
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(cli.psycopg, "connect", lambda url: _FakeConnection())
    monkeypatch.setattr(cli, "run_gate", lambda conn, config: GateResult(config, results, 0.1))
    monkeypatch.setattr(cli, "write_artifacts", lambda gate, out, conn: out)
    assert cli.main(["--as-of", "2026-07-01", "--output-dir", str(tmp_path)]) == expected
