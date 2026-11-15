"""Feature and label logic on hand-built frames: exact values, windows, exclusions and leakage."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from pipeline.transform import NO_EVENT_DAYS, OUTPUT_COLUMNS, TransformParams, build_features

CUTOFF = date(2025, 7, 7)
PARAMS = TransformParams(cutoff_date=CUTOFF, lookback_days=365, horizon_days=90, labeled=True)


def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [1, 2, 3, 4, 5],
            "signup_date": [date(2024, 1, 1)] * 4 + [date(2025, 6, 1)],
            "country": ["US", "DE", "GB", "FR", "IN"],
            "marketing_opt_in": [True, None, False, True, False],
        }
    )


def orders() -> pd.DataFrame:
    rows = [
        # customer 1: active, buys again inside the label window
        (101, 1, "2025-07-01 10:00", "completed", 10.0),
        (102, 1, "2025-06-01 10:00", "completed", 20.0),
        (103, 1, "2025-01-10 10:00", "completed", 30.0),
        (104, 1, "2025-06-20 10:00", "cancelled", 5.0),
        (105, 1, "2025-08-01 10:00", "completed", 99.0),  # label window
        # customer 2: buys exactly at the cutoff: label window, not a feature
        (201, 2, "2024-09-01 10:00", "completed", 50.0),
        (202, 2, "2025-07-07 00:00", "completed", 70.0),
        # customer 3: last order older than the lookback: not in the population
        (301, 3, "2024-06-01 10:00", "completed", 40.0),
        # customer 4: excluded by the DQ gate
        (401, 4, "2025-07-01 10:00", "completed", 15.0),
        # customer 5: one quarantined order; next order lands on cutoff + 90 days (outside the window)
        (501, 5, "2025-05-01 10:00", "completed", 80.0),
        (502, 5, "2025-06-15 10:00", "completed", 25.0),
        (503, 5, "2025-10-05 00:00", "completed", 30.0),
    ]
    frame = pd.DataFrame(rows, columns=["order_id", "customer_id", "order_date", "status", "order_total"])
    frame["order_date"] = pd.to_datetime(frame["order_date"])
    return frame


def events() -> pd.DataFrame:
    rows = [
        (1, date(2025, 7, 6), "view", 3, 1),
        (1, date(2025, 6, 20), "add_to_cart", 1, 1),
        (1, date(2025, 5, 20), "view", 2, 1),  # previous 30 days
        (1, date(2025, 7, 7), "checkout_start", 9, 9),  # cutoff day: must be ignored
        (4, date(2025, 7, 5), "view", 5, 1),  # excluded customer
    ]
    return pd.DataFrame(rows, columns=["customer_id", "day", "event_type", "n_events", "n_sessions"])


def build(params: TransformParams = PARAMS, **overrides) -> pd.DataFrame:
    inputs: dict[str, Any] = {
        "customers": customers(),
        "orders": orders(),
        "events_daily": events(),
        "excluded_customers": {4},
        "quarantined_orders": {501},
    }
    inputs.update(overrides)
    return build_features(params=params, **inputs)


def row(features: pd.DataFrame, customer_id: int) -> dict:
    return features.set_index("customer_id").loc[customer_id].to_dict()


def test_population_is_recent_buyers_minus_excluded_customers():
    features = build()
    assert features["customer_id"].tolist() == [1, 2, 5]
    assert list(features.columns) == list(OUTPUT_COLUMNS)
    assert (features["cutoff_date"] == CUTOFF).all()


def test_order_features_use_the_right_windows():
    r = row(build(), 1)
    assert (r["orders_7d"], r["orders_30d"], r["orders_90d"], r["orders_365d"]) == (1, 1, 2, 3)
    assert (r["spend_7d"], r["spend_30d"], r["spend_90d"], r["spend_365d"]) == (10.0, 10.0, 30.0, 60.0)
    assert r["orders_prev_30d"] == 1  # 2025-06-01 falls in [cutoff - 60, cutoff - 30)
    assert r["order_trend_30d"] == 0
    assert r["days_since_last_order"] == 6
    assert r["avg_order_value_365d"] == 20.0
    assert r["active_days_90d"] == 2
    assert (r["cancelled_90d"], r["returned_90d"]) == (1, 0)


def test_event_features_and_cutoff_day_is_ignored():
    r = row(build(), 1)
    assert (r["views_30d"], r["carts_30d"], r["checkouts_30d"]) == (3, 1, 0)
    assert (r["events_30d"], r["events_prev_30d"], r["event_trend_30d"]) == (4, 2, 2)
    assert r["sessions_30d"] == 2
    assert r["days_since_last_event"] == 1
    assert r["cart_to_view_ratio_30d"] == pytest.approx(0.3333)


def test_customer_without_events_gets_zeros_and_the_no_event_marker():
    r = row(build(), 2)
    assert (r["events_30d"], r["views_30d"], r["sessions_30d"]) == (0, 0, 0)
    assert r["days_since_last_event"] == NO_EVENT_DAYS
    assert r["cart_to_view_ratio_30d"] == 0.0


def test_order_at_the_cutoff_is_label_not_feature():
    r = row(build(), 2)
    assert r["orders_365d"] == 1 and r["spend_365d"] == 50.0
    assert r["churned"] == 0


def test_label_window_is_half_open():
    # customer 5's next order is exactly cutoff + 90 days: outside [cutoff, cutoff + 90)
    assert row(build(), 5)["churned"] == 1
    assert row(build(), 1)["churned"] == 0


def test_quarantined_order_is_ignored():
    r = row(build(), 5)
    assert (r["orders_365d"], r["spend_365d"]) == (1, 25.0)


def test_tenure_starts_at_the_earlier_of_signup_and_first_order():
    assert row(build(), 1)["tenure_days"] == (CUTOFF - date(2024, 1, 1)).days
    assert row(build(), 5)["tenure_days"] == (CUTOFF - date(2025, 6, 1)).days


def test_categoricals_are_explicit_including_unknown_opt_in():
    features = build().set_index("customer_id")
    assert features.loc[[1, 2, 5], "marketing_opt_in"].tolist() == ["yes", "unknown", "no"]
    assert features.loc[[1, 2, 5], "country"].tolist() == ["US", "DE", "IN"]


def test_features_do_not_change_when_anything_on_or_after_the_cutoff_changes():
    """Leakage guard: rewrite every row on or after the cutoff; only the label may change."""
    base = build()
    future_orders = orders()
    after = future_orders["order_date"] >= pd.Timestamp(CUTOFF)
    future_orders.loc[after, "order_total"] = 10_000.0
    future_orders.loc[after, "status"] = "returned"
    extra = pd.DataFrame(
        [(900 + i, 1, pd.Timestamp(CUTOFF) + pd.Timedelta(hours=i), "completed", 1.0) for i in range(50)],
        columns=future_orders.columns,
    )
    future_events = pd.concat(
        [events(), pd.DataFrame([(2, CUTOFF, "view", 500, 50)], columns=events().columns)], ignore_index=True
    )
    changed = build(orders=pd.concat([future_orders, extra], ignore_index=True), events_daily=future_events)
    feature_columns = [c for c in OUTPUT_COLUMNS if c != "churned"]
    pd.testing.assert_frame_equal(base[feature_columns], changed[feature_columns])


def test_unlabeled_cutoff_has_missing_labels():
    params = TransformParams(cutoff_date=CUTOFF, lookback_days=365, horizon_days=90, labeled=False)
    features = build(params)
    assert features["churned"].isna().all()
    assert str(features["churned"].dtype) == "Int64"


def test_output_is_sorted_and_independent_of_input_order():
    shuffled = build(orders=orders().sample(frac=1, random_state=7), customers=customers().iloc[::-1])
    pd.testing.assert_frame_equal(build(), shuffled)
