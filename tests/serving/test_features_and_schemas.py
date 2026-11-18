"""Input validation and the request -> model-frame conversion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.transform import FEATURE_COLUMNS as PIPELINE_FEATURES
from serving.features import ACTIVITY_DEFAULTS, FEATURE_COLUMNS, complete, to_frame
from serving.schemas import MAX_BATCH, CustomerFeatures, PredictRequest
from tests.serving.conftest import customer

REQUIRED = (
    "orders_7d", "orders_30d", "orders_90d", "orders_365d", "spend_7d", "spend_30d", "spend_90d",
    "spend_365d", "orders_prev_30d", "days_since_last_order", "active_days_90d", "tenure_days",
)  # fmt: skip


def test_serving_columns_match_training_exactly():
    assert FEATURE_COLUMNS == PIPELINE_FEATURES


def test_schema_covers_every_model_input():
    fields = set(CustomerFeatures.model_fields) - {"customer_id"}
    assert fields == set(FEATURE_COLUMNS)


def test_minimal_request_gets_activity_defaults_and_derived_fields():
    minimal = {k: v for k, v in customer().items() if k in REQUIRED}
    row = complete(CustomerFeatures(**minimal))
    for name, default in ACTIVITY_DEFAULTS.items():
        assert row[name] == default, name
    assert row["order_trend_30d"] == minimal["orders_30d"] - minimal["orders_prev_30d"]
    assert row["avg_order_value_365d"] == round(minimal["spend_365d"] / minimal["orders_365d"], 2)
    assert row["event_trend_30d"] == 0 and row["cart_to_view_ratio_30d"] == 0.0


def test_provided_values_are_kept():
    row = complete(CustomerFeatures(**customer(views_30d=8, carts_30d=2, country="DE")))
    assert (row["views_30d"], row["carts_30d"], row["country"]) == (8, 2, "DE")
    assert row["cart_to_view_ratio_30d"] == 0.25


def test_frame_has_training_column_order_and_dtypes():
    frame = to_frame([CustomerFeatures(**customer()), CustomerFeatures(**customer(customer_id=None))])
    assert tuple(frame.columns) == FEATURE_COLUMNS
    assert str(frame["orders_365d"].dtype) == "int64"
    assert str(frame["spend_365d"].dtype) == "float64"
    assert frame["country"].tolist() == ["US", "US"]


@pytest.mark.parametrize("missing", REQUIRED)
def test_required_fields_are_required(missing):
    with pytest.raises(ValidationError, match=missing):
        CustomerFeatures(**{k: v for k, v in customer().items() if k != missing})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"orders_365d": 0}, "orders_365d"),  # population: at least one order in the last year
        ({"spend_30d": -1.0}, "spend_30d"),
        ({"days_since_last_order": 0}, "days_since_last_order"),
        ({"days_since_last_order": 400}, "days_since_last_order"),
        ({"orders_7d": 5}, "orders_7d <= orders_30d"),
        ({"spend_90d": 1000.0}, "spend_7d <= spend_30d"),
        ({"active_days_90d": 3}, "active_days_90d"),
        ({"order_trend_30d": 9}, "order_trend_30d"),
        ({"days_since_last_order": 200}, "orders_30d > 0 means"),  # ordered in the last 30 days, yet 200 ago
        ({"country": "usa"}, "country"),
        ({"marketing_opt_in": "maybe"}, "marketing_opt_in"),
        ({"days_since_last_event": 0}, "days_since_last_event"),
        ({"orders_365d": None}, "orders_365d"),
        ({"surprise": 1}, "surprise"),  # unknown fields are rejected, so typos surface
    ],
)  # fmt: skip
def test_invalid_input_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        CustomerFeatures(**customer(**overrides))


def test_batch_size_limits():
    with pytest.raises(ValidationError):
        PredictRequest(instances=[])
    with pytest.raises(ValidationError):
        PredictRequest(instances=[CustomerFeatures(**customer())] * (MAX_BATCH + 1))
    assert len(PredictRequest(instances=[CustomerFeatures(**customer())] * MAX_BATCH).instances) == MAX_BATCH


def test_extreme_but_valid_values_are_accepted():
    big = customer(orders_365d=100_000, orders_90d=50_000, spend_365d=1e9, spend_90d=1e8, tenure_days=36_500)
    frame = to_frame([CustomerFeatures(**big)])
    assert frame.loc[0, "spend_365d"] == 1e9
