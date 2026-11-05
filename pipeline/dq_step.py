"""DQ gate step: run the Part B gate for the snapshot date and export what downstream must exclude."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import psycopg

from data_quality.gate import GateConfig, GateResult, run_gate
from data_quality.report import write_artifacts


class DataQualityBlockedError(RuntimeError):
    """Raised when a block-severity rule fails; the pipeline must stop."""


def run_dq_gate(
    conn: psycopg.Connection, snapshot_date: date, lookback_days: int, report_dir: Path, exclusions_dir: Path
) -> GateResult:
    gate = run_gate(conn, GateConfig(as_of=snapshot_date, lookback_days=lookback_days))
    conn.commit()
    write_artifacts(gate, report_dir, conn)
    export_exclusions(conn, gate.config.run_id, exclusions_dir)
    if not gate.passed:
        blocking = ", ".join(r.rule.rule_id for r in gate.blocking)
        raise DataQualityBlockedError(f"data quality gate {gate.config.run_id} blocked by: {blocking}")
    return gate


def export_exclusions(conn: psycopg.Connection, run_id: str, out_dir: Path) -> None:
    """Excluded customers and quarantined order ids of this run, as Parquet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    customers = conn.execute("SELECT customer_id FROM dq_excluded_customers ORDER BY 1").fetchall()
    orders = conn.execute(
        """
        SELECT DISTINCT (row_key ->> 'order_id')::bigint
        FROM dq_quarantine
        WHERE run_id = %s AND table_name = 'orders'
        ORDER BY 1
        """,
        (run_id,),
    ).fetchall()
    pd.DataFrame(customers, columns=["customer_id"], dtype="int64").to_parquet(
        out_dir / "excluded_customers.parquet", index=False
    )
    pd.DataFrame(orders, columns=["order_id"], dtype="int64").to_parquet(
        out_dir / "quarantined_orders.parquet", index=False
    )
    (out_dir / "dq_run_id.txt").write_text(run_id)


def read_exclusions(in_dir: Path) -> tuple[set[int], set[int], str]:
    customers = set(pd.read_parquet(in_dir / "excluded_customers.parquet")["customer_id"].tolist())
    orders = set(pd.read_parquet(in_dir / "quarantined_orders.parquet")["order_id"].tolist())
    return customers, orders, (in_dir / "dq_run_id.txt").read_text().strip()
