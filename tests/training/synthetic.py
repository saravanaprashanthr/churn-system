"""Synthetic feature tables with the real schema: churn depends on recency, so models can learn it."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from pipeline.transform import FEATURE_COLUMNS
from training.data import Dataset


def synthetic_frame(cutoffs: list[date], rows_per_cutoff: int = 120, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    parts = []
    for i, cutoff in enumerate(cutoffs):
        n = rows_per_cutoff
        orders_365d = rng.integers(1, 12, n)
        orders_90d = np.minimum(orders_365d, rng.integers(0, 6, n))
        orders_30d = np.minimum(orders_90d, rng.integers(0, 3, n))
        orders_7d = np.minimum(orders_30d, rng.integers(0, 2, n))
        days_since = rng.integers(1, 365, n)
        spend_365d = np.round(orders_365d * rng.uniform(100, 400, n), 2)  # real median ~$374
        frame = pd.DataFrame(
            {
                "orders_7d": orders_7d, "orders_30d": orders_30d, "orders_90d": orders_90d,
                "orders_365d": orders_365d,
                "spend_7d": orders_7d * 30.0, "spend_30d": orders_30d * 30.0, "spend_90d": orders_90d * 30.0,
                "spend_365d": spend_365d,
                "orders_prev_30d": rng.integers(0, 3, n), "days_since_last_order": days_since,
                "active_days_90d": orders_90d, "cancelled_90d": rng.integers(0, 2, n),
                "returned_90d": rng.integers(0, 2, n),
                "views_30d": rng.integers(0, 20, n), "carts_30d": rng.integers(0, 5, n),
                "checkouts_30d": rng.integers(0, 3, n), "events_30d": rng.integers(0, 30, n),
                "events_prev_30d": rng.integers(0, 30, n), "sessions_30d": rng.integers(0, 10, n),
                "days_since_last_event": rng.integers(1, 92, n),
                "tenure_days": rng.integers(30, 2000, n),
                "country": rng.choice(["US", "DE", "GB"], n),
                "marketing_opt_in": rng.choice(["yes", "no", "unknown"], n),
            }
        )  # fmt: skip
        frame["order_trend_30d"] = frame["orders_30d"] - frame["orders_prev_30d"]
        frame["event_trend_30d"] = frame["events_30d"] - frame["events_prev_30d"]
        frame["avg_order_value_365d"] = (frame["spend_365d"] / frame["orders_365d"]).round(2)
        frame["cart_to_view_ratio_30d"] = (frame["carts_30d"] / frame["views_30d"].replace(0, np.nan)).fillna(
            0
        )
        churn_logit = (days_since - 120) / 60.0 - 0.3 * orders_90d
        frame["churned"] = (rng.uniform(size=n) < 1 / (1 + np.exp(-churn_logit))).astype("int64")
        frame["customer_id"] = np.arange(n) + 1
        frame["cutoff_date"] = cutoff
        frame["dataset_version"] = f"{cutoff:%Y%m%d}-synthetic{i:03d}"
        parts.append(frame)
    return pd.concat(parts, ignore_index=True)


def synthetic_dataset(start: date = date(2025, 1, 6), weeks: int = 65, rows_per_cutoff: int = 120) -> Dataset:
    cutoffs = [start + timedelta(days=7 * w) for w in range(weeks)]
    frame = synthetic_frame(cutoffs, rows_per_cutoff)
    return Dataset(
        frame=frame,
        feature_columns=FEATURE_COLUMNS,
        versions=tuple(sorted(frame["dataset_version"].unique())),
    )
