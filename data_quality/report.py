"""DQ report (JSON and Markdown) and quarantine sample, written per gate run."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from data_quality.gate import GateResult, Outcome, RuleResult


def _rule_entry(result: RuleResult) -> dict[str, Any]:
    rule = result.rule
    return {
        "rule_id": rule.rule_id,
        "dimension": rule.dimension.value,
        "table": rule.table,
        "severity": rule.severity.value,
        "description": rule.description,
        "rows_checked": result.rows_checked,
        "violations": result.violations,
        "failure_rate": round(result.failure_rate, 6),
        "max_failure_rate": rule.max_failure_rate,
        "outcome": result.outcome.value,
        "quarantined": result.quarantined,
        "seconds": round(result.seconds, 3),
        "sample": result.sample,
    }


def build_report(gate: GateResult, generated_at: datetime | None = None) -> dict[str, Any]:
    outcomes = Counter(r.outcome for r in gate.results)
    by_dimension: dict[str, dict[str, int]] = {}
    for r in gate.results:
        entry = by_dimension.setdefault(r.rule.dimension.value, {"rules": 0, "failed": 0})
        entry["rules"] += 1
        entry["failed"] += r.outcome is not Outcome.PASS
    return {
        "run_id": gate.config.run_id,
        "as_of": gate.config.as_of.isoformat(),
        "lookback_days": gate.config.lookback_days,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "duration_seconds": round(gate.seconds, 1),
        "gate_status": "PASS" if gate.passed else "FAIL",
        "summary": {
            "rules": len(gate.results),
            "passed": outcomes[Outcome.PASS],
            "warnings": outcomes[Outcome.WARN],
            "blocking": outcomes[Outcome.BLOCK],
            "rows_quarantined": sum(r.quarantined for r in gate.results),
            "blocking_rules": [r.rule.rule_id for r in gate.blocking],
            "warning_rules": [r.rule.rule_id for r in gate.warnings],
            "by_dimension": by_dimension,
        },
        "rules": [_rule_entry(r) for r in gate.results],
    }


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# Data quality report `{report['run_id']}`",
        "",
        f"- **Gate status: {report['gate_status']}**",
        f"- as_of: {report['as_of']} (data strictly before this date) · volume lookback: "
        f"{report['lookback_days']} days",
        f"- generated: {report['generated_at']} · duration: {report['duration_seconds']} s",
        f"- rules: {s['rules']} · passed: {s['passed']} · warnings: {s['warnings']} · "
        f"blocking: {s['blocking']} · rows quarantined: {s['rows_quarantined']:,}",
    ]
    if s["blocking_rules"]:
        lines.append(f"- blocking rules: {', '.join(s['blocking_rules'])}")
    lines += [
        "",
        "| Rule | Dimension | Table | Severity | Checked | Violations | Rate | Tolerance | Outcome "
        "| Quarantined |",
        "|---|---|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for r in report["rules"]:
        lines.append(
            f"| `{r['rule_id']}` | {r['dimension']} | {r['table']} | {r['severity']} | "
            f"{r['rows_checked']:,} | {r['violations']:,} | {100 * r['failure_rate']:.3f}% | "
            f"{100 * r['max_failure_rate']:.1f}% | **{r['outcome']}** | {r['quarantined']:,} |"
        )
    lines += [
        "",
        "Outcome: **pass** = within tolerance; **warn** = logged, pipeline continues; "
        "**block** = pipeline stops.",
        "",
    ]
    return "\n".join(lines)


def export_quarantine_sample(
    conn: psycopg.Connection, run_id: str, path: Path, per_rule: int = 10, limit: int = 50
) -> int:
    """Write up to ``per_rule`` quarantined rows of each rule (``limit`` in total) to CSV."""
    rows = conn.execute(
        """
        SELECT run_id, rule_id, table_name, row_key::text, row_data::text
        FROM (
            SELECT q.*, ROW_NUMBER() OVER (PARTITION BY rule_id ORDER BY row_key::text) AS rn
            FROM dq_quarantine q WHERE run_id = %s
        ) ranked
        WHERE rn <= %s
        ORDER BY rule_id, rn
        LIMIT %s
        """,
        (run_id, per_rule, limit),
    ).fetchall()
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["run_id", "rule_id", "table_name", "row_key", "row_data"])
        writer.writerows(rows)
    return len(rows)


def write_artifacts(gate: GateResult, output_dir: Path, conn: psycopg.Connection | None = None) -> Path:
    """Write dq_report.json, dq_report.md and (with a connection) quarantine_sample.csv."""
    run_dir = output_dir / gate.config.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(gate)
    (run_dir / "dq_report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (run_dir / "dq_report.md").write_text(render_markdown(report))
    if conn is not None:
        export_quarantine_sample(conn, gate.config.run_id, run_dir / "quarantine_sample.csv")
    return run_dir
