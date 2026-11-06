"""Load published dataset versions (Part C) and split them by cutoff date."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd
import psycopg

from training.config import SplitConfig

logger = logging.getLogger(__name__)

LABEL = "churned"
CATEGORICAL = ("country", "marketing_opt_in")

DATASET_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (cutoff_date) dataset_version, cutoff_date, feature_columns
        FROM ml_dataset_versions
        WHERE labeled AND snapshot_date = %(snapshot_date)s
          AND lookback_days = %(lookback_days)s AND horizon_days = %(horizon_days)s
        ORDER BY cutoff_date, created_at DESC
    )
    SELECT l.dataset_version, l.cutoff_date, l.feature_columns, f.customer_id, f.features, f.churned
    FROM latest l JOIN ml_customer_features f USING (dataset_version)
    ORDER BY l.cutoff_date, f.customer_id
"""


@dataclass(frozen=True)
class Dataset:
    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    versions: tuple[str, ...]

    @property
    def data_version(self) -> str:
        """One id for the exact set of dataset versions used: logged with every run."""
        return hashlib.sha256("\n".join(sorted(self.versions)).encode()).hexdigest()[:12]

    @property
    def numeric_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.feature_columns if c not in CATEGORICAL)


def load_dataset(
    conn: psycopg.Connection, snapshot_date: date, lookback_days: int, horizon_days: int
) -> Dataset:
    """The newest labeled version of every cutoff for these parameters, features expanded to columns."""
    with conn.cursor() as cursor:
        cursor.execute(
            DATASET_SQL,
            {"snapshot_date": snapshot_date, "lookback_days": lookback_days, "horizon_days": horizon_days},
        )
        rows = cursor.fetchall()
    if not rows:
        raise LookupError("no labeled dataset versions found; run the Part C pipeline backfill first")
    feature_sets = {tuple(r[2]) for r in rows}
    if len(feature_sets) != 1:
        raise ValueError(f"dataset versions disagree on feature columns: {feature_sets}")
    (feature_columns,) = feature_sets
    features = pd.DataFrame([r[4] for r in rows], columns=list(feature_columns))
    meta = pd.DataFrame(
        {
            "dataset_version": [r[0] for r in rows],
            "cutoff_date": [r[1] for r in rows],
            "customer_id": [r[3] for r in rows],
            LABEL: [r[5] for r in rows],
        }
    )
    frame = pd.concat([meta, features], axis=1)
    versions = tuple(sorted(frame["dataset_version"].unique()))
    logger.info("loaded %d rows from %d dataset versions", len(frame), len(versions))
    return Dataset(frame=frame, feature_columns=feature_columns, versions=versions)


def split(dataset: Dataset, config: SplitConfig) -> dict[str, pd.DataFrame]:
    """train / valid / test frames by cutoff date; cutoffs inside the embargo gaps are left out."""
    assigned = dataset.frame["cutoff_date"].map(config.split_of)
    parts = {
        name: dataset.frame[assigned == name].reset_index(drop=True) for name in ("train", "valid", "test")
    }
    for name, part in parts.items():
        if part.empty:
            raise ValueError(f"{name} split is empty: check the backfill covers {getattr(config, name)}")
        logger.info(
            "%s: %d rows, %d cutoffs, churn rate %.3f",
            name, len(part), part["cutoff_date"].nunique(), part[LABEL].mean(),
        )  # fmt: skip
    return parts


def xy(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> tuple[pd.DataFrame, pd.Series]:
    return frame[list(feature_columns)], frame[LABEL].astype("int64")
