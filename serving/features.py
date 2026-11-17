"""Turn validated requests into the exact frame the model was trained on.

Column names and order must match training (``pipeline.transform.FEATURE_COLUMNS``); a unit test keeps the
two lists identical without making the serving image depend on the pipeline package.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from serving.schemas import CustomerFeatures

FEATURE_COLUMNS: tuple[str, ...] = (
    "orders_7d", "orders_30d", "orders_90d", "orders_365d",
    "spend_7d", "spend_30d", "spend_90d", "spend_365d",
    "orders_prev_30d", "order_trend_30d", "days_since_last_order", "avg_order_value_365d",
    "active_days_90d", "cancelled_90d", "returned_90d",
    "views_30d", "carts_30d", "checkouts_30d", "events_30d", "events_prev_30d", "event_trend_30d",
    "sessions_30d", "days_since_last_event", "cart_to_view_ratio_30d",
    "tenure_days", "country", "marketing_opt_in",
)  # fmt: skip

CATEGORICAL = ("country", "marketing_opt_in")
FLOAT_COLUMNS = (
    "spend_7d",
    "spend_30d",
    "spend_90d",
    "spend_365d",
    "avg_order_value_365d",
    "cart_to_view_ratio_30d",
)

# "No activity" defaults for optional fields; days_since_last_event uses the pipeline's "no event in the
# 90-day window" value (91).
ACTIVITY_DEFAULTS: dict[str, int | str] = {
    "cancelled_90d": 0,
    "returned_90d": 0,
    "views_30d": 0,
    "carts_30d": 0,
    "checkouts_30d": 0,
    "events_30d": 0,
    "events_prev_30d": 0,
    "sessions_30d": 0,
    "days_since_last_event": 91,
    "country": "unknown",
    "marketing_opt_in": "unknown",
}


def complete(features: CustomerFeatures) -> dict[str, int | float | str]:
    """All 27 model inputs for one customer: defaults for missing activity, derived fields computed."""
    row = features.model_dump(exclude={"customer_id"})
    for name, default in ACTIVITY_DEFAULTS.items():
        if row[name] is None:
            row[name] = default
    if row["order_trend_30d"] is None:
        row["order_trend_30d"] = row["orders_30d"] - row["orders_prev_30d"]
    if row["event_trend_30d"] is None:
        row["event_trend_30d"] = row["events_30d"] - row["events_prev_30d"]
    if row["avg_order_value_365d"] is None:
        row["avg_order_value_365d"] = round(row["spend_365d"] / row["orders_365d"], 2)
    if row["cart_to_view_ratio_30d"] is None:
        views = row["views_30d"]
        row["cart_to_view_ratio_30d"] = round(row["carts_30d"] / views, 4) if views else 0.0
    return row


def to_frame(instances: Sequence[CustomerFeatures]) -> pd.DataFrame:
    frame = pd.DataFrame([complete(f) for f in instances], columns=list(FEATURE_COLUMNS))
    for column in FEATURE_COLUMNS:
        if column in CATEGORICAL:
            frame[column] = frame[column].astype(str)
        elif column in FLOAT_COLUMNS:
            frame[column] = frame[column].astype("float64")
        else:
            frame[column] = frame[column].astype("int64")
    return frame
