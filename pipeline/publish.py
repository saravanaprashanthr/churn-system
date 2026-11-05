"""Publish step: write the validated feature table as a versioned, content-addressed dataset.

``dataset_version`` = ``<cutoff YYYYMMDD>-<first 12 hex of sha256>``, where the hash covers the
parameters and a canonical CSV rendering of the rows (sorted by customer, fixed column order and
float format). Publishing the same inputs again finds the existing version and writes nothing,
so re-runs and backfills are idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from importlib import resources

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from pipeline.transform import FEATURE_COLUMNS, LABEL_COLUMN, OUTPUT_COLUMNS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetParams:
    cutoff_date: date
    snapshot_date: date
    lookback_days: int
    horizon_days: int
    labeled: bool


def canonical_bytes(features: pd.DataFrame) -> bytes:
    frame = features[list(OUTPUT_COLUMNS)].sort_values("customer_id", ignore_index=True)
    return frame.to_csv(index=False, float_format="%.6f", lineterminator="\n").encode()


def content_hash(features: pd.DataFrame, params: DatasetParams) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps({k: str(v) for k, v in asdict(params).items()}, sort_keys=True).encode())
    digest.update(json.dumps(list(OUTPUT_COLUMNS)).encode())
    digest.update(canonical_bytes(features))
    return digest.hexdigest()


def dataset_version(features: pd.DataFrame, params: DatasetParams) -> tuple[str, str]:
    full_hash = content_hash(features, params)
    return f"{params.cutoff_date:%Y%m%d}-{full_hash[:12]}", full_hash


def ensure_tables(conn: psycopg.Connection) -> None:
    conn.execute(resources.files("pipeline").joinpath("ddl.sql").read_text())


def publish(
    conn: psycopg.Connection, features: pd.DataFrame, params: DatasetParams, dq_run_id: str, code_version: str
) -> tuple[str, bool]:
    """Write the dataset unless this exact version already exists. Returns (version, created)."""
    version, full_hash = dataset_version(features, params)
    churn_rate = float(features[LABEL_COLUMN].mean()) if params.labeled and len(features) else None
    with conn.transaction():
        ensure_tables(conn)
        inserted = conn.execute(
            """
            INSERT INTO ml_dataset_versions (
                dataset_version, content_hash, cutoff_date, snapshot_date, lookback_days, horizon_days,
                labeled, n_rows, churn_rate, feature_columns, dq_run_id, code_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dataset_version) DO NOTHING
            """,
            (
                version,
                full_hash,
                params.cutoff_date,
                params.snapshot_date,
                params.lookback_days,
                params.horizon_days,
                params.labeled,
                len(features),
                churn_rate,
                Jsonb(list(FEATURE_COLUMNS)),
                dq_run_id,
                code_version,
            ),  # fmt: skip
        ).rowcount
        if not inserted:
            logger.info("dataset %s already published; nothing written", version)
            return version, False
        records = json.loads(features[list(FEATURE_COLUMNS)].to_json(orient="records"))
        labels = features[LABEL_COLUMN].astype("Int64")
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO ml_customer_features "
                "(dataset_version, customer_id, cutoff_date, features, churned) VALUES (%s, %s, %s, %s, %s)",
                [
                    (version, int(cid), params.cutoff_date, Jsonb(rec), None if pd.isna(lab) else int(lab))
                    for cid, rec, lab in zip(features["customer_id"], records, labels, strict=True)
                ],
            )
    logger.info("published dataset %s: %d rows", version, len(features))
    return version, True
