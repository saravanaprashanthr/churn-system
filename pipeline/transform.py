"""Transform step: point-in-time features and the 90-day churn label for one cutoff.

Point-in-time rule: every feature uses rows strictly before ``cutoff_date`` only. The label uses
completed orders in ``[cutoff_date, cutoff_date + horizon_days)``. Orders are split into the two
windows first and the feature code never sees the label window (``tests/pipeline`` checks that
changing anything on or after the cutoff leaves every feature unchanged).

Population: customers not excluded by the DQ gate with at least one completed, non-quarantined
order in the ``lookback_days`` before the cutoff. Customers who never bought cannot churn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from pipeline.extract import EVENT_LOOKBACK_DAYS

COMPLETED = "completed"
ORDER_WINDOWS = (7, 30, 90, 365)

FEATURE_COLUMNS: tuple[str, ...] = (
    "orders_7d", "orders_30d", "orders_90d", "orders_365d",
    "spend_7d", "spend_30d", "spend_90d", "spend_365d",
    "orders_prev_30d", "order_trend_30d", "days_since_last_order", "avg_order_value_365d",
    "active_days_90d", "cancelled_90d", "returned_90d",
    "views_30d", "carts_30d", "checkouts_30d", "events_30d", "events_prev_30d", "event_trend_30d",
    "sessions_30d", "days_since_last_event", "cart_to_view_ratio_30d",
    "tenure_days", "country", "marketing_opt_in",
)  # fmt: skip
CATEGORICAL_COLUMNS: tuple[str, ...] = ("country", "marketing_opt_in")
COUNT_COLUMNS: tuple[str, ...] = (
    "orders_7d", "orders_30d", "orders_90d", "orders_365d", "orders_prev_30d",
    "active_days_90d", "cancelled_90d", "returned_90d",
    "views_30d", "carts_30d", "checkouts_30d", "events_30d", "events_prev_30d", "sessions_30d",
)  # fmt: skip
SPEND_COLUMNS: tuple[str, ...] = tuple(f"spend_{days}d" for days in ORDER_WINDOWS)
LABEL_COLUMN = "churned"
KEY_COLUMNS: tuple[str, ...] = ("customer_id", "cutoff_date")
OUTPUT_COLUMNS: tuple[str, ...] = (*KEY_COLUMNS, *FEATURE_COLUMNS, LABEL_COLUMN)

# Value used for days_since_last_event when the customer had no event in the event lookback window.
NO_EVENT_DAYS = EVENT_LOOKBACK_DAYS + 1


@dataclass(frozen=True)
class TransformParams:
    cutoff_date: date
    lookback_days: int
    horizon_days: int
    labeled: bool


def _days_before(cutoff: pd.Timestamp, ts: pd.Series) -> pd.Series:
    return (cutoff - ts).dt.days


def _order_features(history: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    completed = history[history["status"] == COMPLETED].copy()
    completed["age"] = _days_before(cutoff, completed["order_date"])
    completed["day"] = completed["order_date"].dt.floor("D")
    grouped = completed.groupby("customer_id")
    out = pd.DataFrame(index=pd.Index(grouped.size().index, name="customer_id"))
    for days in ORDER_WINDOWS:
        in_window = completed[completed["order_date"] >= cutoff - pd.Timedelta(days=days)]
        by_customer = in_window.groupby("customer_id")
        out[f"orders_{days}d"] = by_customer.size()
        out[f"spend_{days}d"] = by_customer["order_total"].sum()
    prev = completed[
        (completed["order_date"] >= cutoff - pd.Timedelta(days=60))
        & (completed["order_date"] < cutoff - pd.Timedelta(days=30))
    ]
    out["orders_prev_30d"] = prev.groupby("customer_id").size()
    out["days_since_last_order"] = (cutoff - grouped["day"].max()).dt.days
    recent = completed[completed["order_date"] >= cutoff - pd.Timedelta(days=90)]
    out["active_days_90d"] = recent.groupby("customer_id")["day"].nunique()
    out["first_order_day"] = grouped["day"].min()

    other = history[history["order_date"] >= cutoff - pd.Timedelta(days=90)]
    for status in ("cancelled", "returned"):
        out[f"{status}_90d"] = other[other["status"] == status].groupby("customer_id").size()
    return out


def _event_features(events: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    events = events.copy()
    events["day"] = pd.to_datetime(events["day"])
    events["age"] = _days_before(cutoff, events["day"])
    last_30 = events[events["age"] <= 30]
    prev_30 = events[(events["age"] > 30) & (events["age"] <= 60)]

    def by_type(frame: pd.DataFrame, event_type: str) -> pd.Series:
        return frame[frame["event_type"] == event_type].groupby("customer_id")["n_events"].sum()

    out = pd.DataFrame(index=pd.Index(events["customer_id"].unique(), name="customer_id"))
    out["views_30d"] = by_type(last_30, "view")
    out["carts_30d"] = by_type(last_30, "add_to_cart")
    out["checkouts_30d"] = by_type(last_30, "checkout_start")
    out["events_30d"] = last_30.groupby("customer_id")["n_events"].sum()
    out["events_prev_30d"] = prev_30.groupby("customer_id")["n_events"].sum()
    out["sessions_30d"] = last_30.groupby("customer_id")["n_sessions"].sum()
    out["days_since_last_event"] = events.groupby("customer_id")["age"].min()
    return out


def build_features(
    customers: pd.DataFrame,
    orders: pd.DataFrame,
    events_daily: pd.DataFrame,
    excluded_customers: set[int],
    quarantined_orders: set[int],
    params: TransformParams,
) -> pd.DataFrame:
    cutoff = pd.Timestamp(params.cutoff_date)
    label_end = cutoff + pd.Timedelta(days=params.horizon_days)
    lookback_start = cutoff - pd.Timedelta(days=params.lookback_days)

    orders = orders.copy()
    orders["order_date"] = pd.to_datetime(orders["order_date"])
    orders["order_total"] = orders["order_total"].astype("float64")
    orders = orders[
        orders["order_date"].notna()
        & ~orders["order_id"].isin(quarantined_orders)
        & ~orders["customer_id"].isin(excluded_customers)
    ]
    history = orders[(orders["order_date"] >= lookback_start) & (orders["order_date"] < cutoff)]
    future = orders[(orders["order_date"] >= cutoff) & (orders["order_date"] < label_end)]
    events = events_daily[
        (pd.to_datetime(events_daily["day"]) < cutoff) & ~events_daily["customer_id"].isin(excluded_customers)
    ]

    feats = _order_features(history, cutoff)
    feats = feats.join(_event_features(events, cutoff), how="left")

    feats[list(COUNT_COLUMNS)] = feats[list(COUNT_COLUMNS)].fillna(0).astype("int64")
    feats[list(SPEND_COLUMNS)] = feats[list(SPEND_COLUMNS)].fillna(0.0).round(2)
    feats["order_trend_30d"] = feats["orders_30d"] - feats["orders_prev_30d"]
    feats["event_trend_30d"] = feats["events_30d"] - feats["events_prev_30d"]
    feats["avg_order_value_365d"] = (feats["spend_365d"] / feats["orders_365d"]).round(2)
    feats["cart_to_view_ratio_30d"] = (
        (feats["carts_30d"] / feats["views_30d"].replace(0, np.nan)).fillna(0.0).round(4)
    )
    feats["days_since_last_event"] = feats["days_since_last_event"].fillna(NO_EVENT_DAYS).astype("int64")

    attrs = customers.set_index("customer_id")
    feats = feats.join(attrs[["signup_date", "country", "marketing_opt_in"]], how="left")
    signup = pd.to_datetime(feats["signup_date"])
    start = pd.concat([signup, feats["first_order_day"]], axis=1).min(axis=1)  # signup_date is unreliable
    feats["tenure_days"] = (cutoff - start).dt.days.astype("int64")
    feats["country"] = feats["country"].fillna("unknown").astype("str")
    feats["marketing_opt_in"] = feats["marketing_opt_in"].map({True: "yes", False: "no"}).fillna("unknown")

    if params.labeled:
        buyers = set(future.loc[future["status"] == COMPLETED, "customer_id"])
        feats[LABEL_COLUMN] = (~feats.index.isin(buyers)).astype("int64")
    else:
        feats[LABEL_COLUMN] = pd.array([pd.NA] * len(feats), dtype="Int64")

    feats["cutoff_date"] = cutoff.date()
    result = feats.reset_index()[list(OUTPUT_COLUMNS)]
    return result.sort_values("customer_id", ignore_index=True)
