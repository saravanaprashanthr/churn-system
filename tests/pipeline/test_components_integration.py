"""Each component's real function, chained against a seeded Postgres schema (no KFP runtime needed).

Seed: the steady DQ seed (70 days before AS_OF = 2026-03-01) plus a few hand-placed orders around the
cutoff 2025-12-01 that decide the population and the labels:

    customer 1: completed 2025-11-20, then orders daily from 2025-12-21  -> in population, churned 0
    customer 4: completed 2025-10-01 and 2025-12-25                       -> in population, churned 0
    customer 5: completed 2025-06-01, cancelled 2025-11-25, nothing after -> in population, churned 1
    customer 2: only a completed order on 2024-11-15 (before the lookback) -> not extracted, not in population
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

import pandas as pd
import psycopg
import pytest
from kfp.dsl import Artifact, Dataset

from pipeline.components import dq_gate_op, extract_op, publish_op, transform_op, validate_op
from pipeline.dq_step import DataQualityBlockedError
from tests.seed import AS_OF

pytestmark = pytest.mark.integration

CUTOFF = date(2025, 12, 1)
WINDOW = {"cutoff_date": CUTOFF.isoformat(), "snapshot_date": AS_OF.isoformat(), "lookback_days": 365,
          "horizon_days": 90}  # fmt: skip
EXTRA_ORDERS = [
    (8001, 1, "2025-11-20 09:00", "completed"),
    (8002, 4, "2025-10-01 09:00", "completed"),
    (8003, 4, "2025-12-25 09:00", "completed"),
    (8004, 5, "2025-06-01 09:00", "completed"),
    (8005, 5, "2025-11-25 09:00", "cancelled"),
    (8006, 2, "2024-11-15 09:00", "completed"),
]


@pytest.fixture
def pipeline_db(seeded: psycopg.Connection) -> str:
    for order_id, customer_id, ts, status in EXTRA_ORDERS:
        seeded.execute(
            "INSERT INTO orders VALUES (%s, %s, %s, %s, 20.00)", (order_id, customer_id, ts, status)
        )
        seeded.execute("INSERT INTO order_items VALUES (%s, %s, 1, 2, 10.00)", (order_id, order_id))
    seeded.execute("INSERT INTO events VALUES (70001, 4, '2025-11-28 10:00', 'view', 1, 'four')")
    seeded.commit()
    row = seeded.execute("SELECT current_schema()").fetchone()
    assert row is not None
    schema = row[0]
    info = seeded.info
    return (
        f"postgresql://{info.user}:{quote(info.password)}@{info.host}:{info.port}/{info.dbname}"
        f"?options={quote(f'-csearch_path={schema}')}"
    )


def artifact(cls, tmp_path, name):
    return cls(name=name, uri=str(tmp_path / name), metadata={})


def run_chain(url: str, tmp_path) -> dict:
    raw = artifact(Dataset, tmp_path, "raw")
    extract_op.python_func(source_db_url=url, raw=raw, **WINDOW)
    report, exclusions = artifact(Artifact, tmp_path, "report"), artifact(Dataset, tmp_path, "exclusions")
    dq_run_id = dq_gate_op.python_func(
        source_db_url=url,
        snapshot_date=AS_OF.isoformat(),
        dq_lookback_days=28,
        report=report,
        exclusions=exclusions,
    )
    features = artifact(Dataset, tmp_path, "features")
    transform_op.python_func(raw=raw, exclusions=exclusions, features=features, **WINDOW)
    validation = artifact(Artifact, tmp_path, "validation.json")
    validate_op.python_func(features=features, min_rows=1, validation=validation, **WINDOW)
    dataset = artifact(Dataset, tmp_path, "dataset")
    version = publish_op.python_func(
        features=features,
        validation=validation,
        source_db_url=url,
        dq_run_id=dq_run_id,
        dataset=dataset,
        **WINDOW,
    )
    return {"raw": raw, "report": report, "features": features, "dataset": dataset, "version": version,
            "dq_run_id": dq_run_id}  # fmt: skip


def test_extract_respects_the_windows(pipeline_db, tmp_path):
    raw = artifact(Dataset, tmp_path, "raw")
    extract_op.python_func(source_db_url=pipeline_db, raw=raw, **WINDOW)
    orders = pd.read_parquet(tmp_path / "raw" / "orders.parquet")
    assert 8006 not in set(orders["order_id"])  # before cutoff - lookback
    assert orders["order_date"].min() >= pd.Timestamp("2024-12-01")
    assert orders["order_date"].max() < pd.Timestamp(AS_OF)
    events = pd.read_parquet(tmp_path / "raw" / "events_daily.parquet")
    assert events.to_dict("records") == [
        {"customer_id": 4, "day": date(2025, 11, 28), "event_type": "view", "n_events": 1, "n_sessions": 1}
    ]
    assert raw.metadata["labeled"] is True


def test_full_chain_publishes_a_versioned_dataset(pipeline_db, tmp_path):
    out = run_chain(pipeline_db, tmp_path)
    features = pd.read_parquet(tmp_path / "features" / "features.parquet").set_index("customer_id")
    assert features["churned"].to_dict() == {1: 0, 4: 0, 5: 1}
    assert features.loc[5, "cancelled_90d"] == 1
    assert features.loc[4, "views_30d"] == 1
    assert out["version"].startswith("20251201-")
    assert out["dataset"].metadata["created"] is True
    assert out["report"].metadata["status"] == "PASS"

    with psycopg.connect(pipeline_db) as conn:
        version_row = conn.execute(
            "SELECT n_rows, churn_rate, labeled, dq_run_id FROM ml_dataset_versions "
            "WHERE dataset_version = %s",
            (out["version"],),
        ).fetchone()
        stored = conn.execute(
            "SELECT customer_id, churned, (features ->> 'orders_365d')::int FROM ml_customer_features "
            "WHERE dataset_version = %s ORDER BY 1",
            (out["version"],),
        ).fetchall()
    assert version_row == (3, pytest.approx(1 / 3), True, "dq_20260301")
    assert stored == [(1, 0, 1), (4, 0, 1), (5, 1, 1)]


def test_rerun_is_idempotent(pipeline_db, tmp_path):
    first = run_chain(pipeline_db, tmp_path / "first")
    second = run_chain(pipeline_db, tmp_path / "second")
    assert second["version"] == first["version"]
    assert second["dataset"].metadata["created"] is False
    with psycopg.connect(pipeline_db) as conn:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM ml_dataset_versions), (SELECT COUNT(*) FROM ml_customer_features)"
        ).fetchone()
    assert counts == (1, 3)


def test_blocked_gate_stops_the_chain(pipeline_db, tmp_path):
    with psycopg.connect(pipeline_db) as conn:
        conn.execute(
            "DELETE FROM order_items WHERE order_id IN "
            "(SELECT order_id FROM orders WHERE order_date >= '2026-02-20')"
        )
        conn.execute("DELETE FROM orders WHERE order_date >= '2026-02-20'")
    with pytest.raises(DataQualityBlockedError, match="ORD_FRESHNESS"):
        dq_gate_op.python_func(
            source_db_url=pipeline_db, snapshot_date=AS_OF.isoformat(), dq_lookback_days=28,
            report=artifact(Artifact, tmp_path, "report"),
            exclusions=artifact(Dataset, tmp_path, "exclusions"),
        )  # fmt: skip
