"""Run the rule catalog against a Postgres database and record the results.

The gate is idempotent per run date: the run id is derived from ``as_of``, and a re-run first
deletes that run's quarantine rows and results, then writes them again in one transaction.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from data_quality.rules import RULES, Rule, Severity

logger = logging.getLogger(__name__)

SAMPLE_SIZE = 5


class Outcome(StrEnum):
    PASS = "pass"  # noqa: S105 - an outcome name, not a password
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class GateConfig:
    as_of: date
    lookback_days: int = 28

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")

    @property
    def run_id(self) -> str:
        return f"dq_{self.as_of:%Y%m%d}"

    @property
    def sql_params(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "lookback_days": self.lookback_days}


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    rows_checked: int
    violations: int
    failure_rate: float
    outcome: Outcome
    quarantined: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0


@dataclass(frozen=True)
class GateResult:
    config: GateConfig
    results: list[RuleResult]
    seconds: float

    @property
    def passed(self) -> bool:
        return not self.blocking

    @property
    def blocking(self) -> list[RuleResult]:
        return [r for r in self.results if r.outcome is Outcome.BLOCK]

    @property
    def warnings(self) -> list[RuleResult]:
        return [r for r in self.results if r.outcome is Outcome.WARN]


def evaluate(rule: Rule, rows_checked: int, violations: int) -> tuple[float, Outcome]:
    """Failure rate and outcome of a rule, from its counts.

    A rule fails when its failure rate exceeds ``max_failure_rate``; a failing rule blocks or
    warns according to its severity. With nothing checked, any violation counts as a full failure.
    """
    if rows_checked < 0 or violations < 0:
        raise ValueError("counts must be non-negative")
    if rows_checked == 0:
        rate = 1.0 if violations else 0.0
    else:
        rate = violations / rows_checked
    if rate <= rule.max_failure_rate:
        return rate, Outcome.PASS
    return rate, Outcome.BLOCK if rule.severity is Severity.BLOCK else Outcome.WARN


def ensure_tables(conn: psycopg.Connection) -> None:
    ddl = resources.files("data_quality").joinpath("ddl.sql").read_text()
    conn.execute(ddl)


def _scalar(conn: psycopg.Connection, query: str, params: dict[str, Any]) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _quarantine(conn: psycopg.Connection, rule: Rule, run_id: str, params: dict[str, Any]) -> int:
    key_object = sql.SQL(", ").join(
        sql.SQL("{}, v.{}").format(sql.Literal(column), sql.Identifier(column)) for column in rule.key_columns
    )
    statement = sql.SQL(
        "INSERT INTO dq_quarantine (run_id, rule_id, table_name, row_key, row_data) "
        "SELECT %(run_id)s, %(rule_id)s, %(table_name)s, jsonb_build_object({keys}), to_jsonb(v) "
        "FROM ({violations}) AS v"
    ).format(keys=key_object, violations=sql.SQL(rule.violations_sql))
    cursor = conn.execute(
        statement, {**params, "run_id": run_id, "rule_id": rule.rule_id, "table_name": rule.table}
    )
    return cursor.rowcount


def run_rule(conn: psycopg.Connection, rule: Rule, config: GateConfig) -> RuleResult:
    started = time.perf_counter()
    params = config.sql_params
    rows_checked = _scalar(conn, rule.checked_sql, params)
    # Rule SQL comes from our own catalog (rules.py), never from input; values are bound parameters.
    violations = _scalar(conn, f"SELECT COUNT(*) FROM ({rule.violations_sql}) AS v", params)  # noqa: S608
    rate, outcome = evaluate(rule, rows_checked, violations)

    sample: list[dict[str, Any]] = []
    quarantined = 0
    if violations:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT * FROM ({rule.violations_sql}) AS v LIMIT {SAMPLE_SIZE}", params)  # noqa: S608
            sample = cursor.fetchall()
        if rule.quarantine:
            quarantined = _quarantine(conn, rule, config.run_id, params)

    seconds = time.perf_counter() - started
    log = logger.warning if outcome is not Outcome.PASS else logger.info
    log(
        "%s %s: %d/%d violations (%.4f%%, tolerance %.2f%%), quarantined %d, %.2fs",
        rule.rule_id,
        outcome.value,
        violations,
        rows_checked,
        100 * rate,
        100 * rule.max_failure_rate,
        quarantined,
        seconds,
    )
    return RuleResult(rule, rows_checked, violations, rate, outcome, quarantined, sample, seconds)


def _record(conn: psycopg.Connection, run_id: str, result: RuleResult) -> None:
    rule = result.rule
    conn.execute(
        """
        INSERT INTO dq_rule_results (run_id, rule_id, dimension, table_name, severity, rows_checked,
                                     violations, failure_rate, max_failure_rate, outcome, quarantined)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            rule.rule_id,
            rule.dimension.value,
            rule.table,
            rule.severity.value,
            result.rows_checked,
            result.violations,
            result.failure_rate,
            rule.max_failure_rate,
            result.outcome.value,
            result.quarantined,
        ),
    )


def _refresh_excluded_customers(conn: psycopg.Connection, run_id: str, rules: tuple[Rule, ...]) -> int:
    """Replace dq_excluded_customers with the customers quarantined by excluding rules in this run."""
    excluding = [r.rule_id for r in rules if r.excludes_customers]
    conn.execute("DELETE FROM dq_excluded_customers")
    if not excluding:
        return 0
    cursor = conn.execute(
        """
        INSERT INTO dq_excluded_customers (customer_id, orders_in_window, rule_id, reason, flagged_at)
        SELECT DISTINCT ON ((q.row_key ->> 'customer_id')::bigint)
               (q.row_key ->> 'customer_id')::bigint,
               (q.row_data ->> 'orders_in_window')::bigint,
               q.rule_id,
               'quarantined by rule ' || q.rule_id || ' in run ' || q.run_id,
               q.quarantined_at
        FROM dq_quarantine q
        WHERE q.run_id = %s AND q.rule_id = ANY(%s)
        ORDER BY (q.row_key ->> 'customer_id')::bigint, q.rule_id
        """,
        (run_id, excluding),
    )
    return cursor.rowcount


def run_gate(conn: psycopg.Connection, config: GateConfig, rules: tuple[Rule, ...] = RULES) -> GateResult:
    """Evaluate all rules in one transaction and persist quarantine rows and results."""
    started = time.perf_counter()
    logger.info("data quality gate %s: %d rules, as_of=%s", config.run_id, len(rules), config.as_of)
    with conn.transaction():
        ensure_tables(conn)
        conn.execute("DELETE FROM dq_quarantine WHERE run_id = %s", (config.run_id,))
        conn.execute("DELETE FROM dq_rule_results WHERE run_id = %s", (config.run_id,))
        results = []
        for rule in rules:
            result = run_rule(conn, rule, config)
            _record(conn, config.run_id, result)
            results.append(result)
        excluded = _refresh_excluded_customers(conn, config.run_id, rules)
    gate = GateResult(config, results, time.perf_counter() - started)
    logger.info(
        "data quality gate %s: %s, %d blocking, %d warnings, %d excluded customers, %.1fs",
        config.run_id,
        "PASS" if gate.passed else "FAIL",
        len(gate.blocking),
        len(gate.warnings),
        excluded,
        gate.seconds,
    )
    return gate
