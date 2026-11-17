"""Model behavior tests: run against a registered model version and the real holdout split.

    MODEL_URI=models:/churn-90d/3 pytest -m model tests/model

Part of the promotion gate (training/promote.py): a candidate that fails any of these is never promoted.
Needs MLFLOW_TRACKING_URI and DATABASE_URL; skipped when either service is unreachable.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import average_precision_score, roc_auc_score

from serving.features import to_frame
from serving.schemas import CustomerFeatures
from serving.smoke import SAMPLE_CUSTOMER
from training.config import TrainConfig
from training.metrics import lift_at_k

pytestmark = pytest.mark.model

# Stated floors and tolerances (documented in deploy/RUNBOOK.md and the model card).
MIN_PR_AUC = 0.85
MIN_ROC_AUC = 0.80
MIN_LIFT_AT_10PCT = 1.2
DIRECTION_TOLERANCE = 0.005  # max allowed probability increase for a change that should lower churn
CALIBRATION_TOLERANCE = 0.05
SAMPLE_SIZE = 1000


@pytest.fixture(scope="module")
def model():
    import mlflow

    uri = os.environ.get("MODEL_URI", "models:/churn-90d@candidate")
    try:
        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"))
        return mlflow.sklearn.load_model(uri)
    except Exception as exc:  # noqa: BLE001 - any registry problem means "cannot run here"
        pytest.skip(f"model {uri} not loadable: {exc}")


@pytest.fixture(scope="module")
def holdout() -> tuple[pd.DataFrame, pd.Series]:
    import psycopg

    from training.data import load_dataset, split, xy

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    config = TrainConfig()
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            dataset = load_dataset(
                conn, config.snapshot_date, config.lookback_days, config.split.horizon_days
            )
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unreachable: {exc}")
    return xy(split(dataset, config.split)["test"], dataset.feature_columns)


@pytest.fixture(scope="module")
def sample(holdout) -> pd.DataFrame:
    return holdout[0].sample(SAMPLE_SIZE, random_state=0).reset_index(drop=True)


def proba(model, frame: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(frame)[:, 1]


# ---------------------------------------------------------------- minimum performance
def test_minimum_performance_on_holdout(model, holdout):
    X, y = holdout
    scores = proba(model, X)
    assert average_precision_score(y, scores) >= MIN_PR_AUC
    assert roc_auc_score(y, scores) >= MIN_ROC_AUC
    assert lift_at_k(y.to_numpy(), scores, 0.1) >= MIN_LIFT_AT_10PCT


def test_scores_are_roughly_calibrated(model, holdout):
    X, y = holdout
    assert abs(proba(model, X).mean() - y.mean()) <= CALIBRATION_TOLERANCE


# ---------------------------------------------------------------- directional
def _assert_not_higher(before: np.ndarray, after: np.ndarray, what: str) -> None:
    increase = after - before
    assert increase.max() <= DIRECTION_TOLERANCE, f"{what}: probability rose by up to {increase.max():.4f}"
    assert increase.mean() <= 0, f"{what}: probability rose on average ({increase.mean():+.4f})"


def test_more_recent_last_order_does_not_raise_churn(model, sample):
    recent = sample.assign(days_since_last_order=(sample["days_since_last_order"] - 30).clip(lower=1))
    _assert_not_higher(proba(model, sample), proba(model, recent), "last order 30 days more recent")


def test_older_last_order_does_not_lower_churn(model, sample):
    older = sample.assign(days_since_last_order=(sample["days_since_last_order"] + 30).clip(upper=365))
    _assert_not_higher(proba(model, older), proba(model, sample), "last order 30 days older (reversed)")


def test_an_extra_recent_order_does_not_raise_churn(model, sample):
    more = sample.assign(
        orders_30d=sample["orders_30d"] + 1,
        orders_90d=sample["orders_90d"] + 1,
        orders_365d=sample["orders_365d"] + 1,
        active_days_90d=sample["active_days_90d"] + 1,
        order_trend_30d=sample["order_trend_30d"] + 1,
    )
    _assert_not_higher(proba(model, sample), proba(model, more), "one more order in the last 30 days")


# ---------------------------------------------------------------- invariance
def test_column_order_does_not_matter(model, sample):
    shuffled = sample[list(reversed(sample.columns))]
    np.testing.assert_allclose(proba(model, shuffled), proba(model, sample))


def test_batch_and_single_scoring_agree(model, sample):
    batch = proba(model, sample.head(20))
    single = np.array([proba(model, sample.iloc[[i]])[0] for i in range(20)])
    np.testing.assert_allclose(single, batch)


def test_explicit_defaults_equal_omitted_fields(model):
    omitted = CustomerFeatures(**{k: v for k, v in SAMPLE_CUSTOMER.items() if not k.endswith("_30d")
                                  or k.startswith(("orders", "spend"))})  # fmt: skip
    explicit = CustomerFeatures(**omitted.model_dump() | {"views_30d": 0, "carts_30d": 0, "checkouts_30d": 0,
                                                          "events_30d": 0, "sessions_30d": 0})  # fmt: skip
    np.testing.assert_allclose(proba(model, to_frame([omitted])), proba(model, to_frame([explicit])))


# ---------------------------------------------------------------- no crash on edge cases
EDGE_CASES: dict[str, dict[str, Any]] = {
    "only required fields (all optional ones null)": {
        k: SAMPLE_CUSTOMER[k]
        for k in ("orders_7d", "orders_30d", "orders_90d", "orders_365d", "spend_7d", "spend_30d",
                  "spend_90d", "spend_365d", "orders_prev_30d", "days_since_last_order", "active_days_90d",
                  "tenure_days")
    },  # fmt: skip
    "extreme volumes and spend": {
        **SAMPLE_CUSTOMER, "orders_7d": 1000, "orders_30d": 5000, "orders_90d": 20000, "orders_365d": 100_000,
        "orders_prev_30d": 5000, "active_days_90d": 90, "spend_7d": 1e6, "spend_30d": 1e7, "spend_90d": 1e8,
        "spend_365d": 1e9, "tenure_days": 36_500, "views_30d": 100_000, "events_30d": 100_000,
    },
    "one order a year ago, no activity since": {
        **SAMPLE_CUSTOMER, "orders_7d": 0, "orders_30d": 0, "orders_90d": 0, "orders_365d": 1,
        "orders_prev_30d": 0, "active_days_90d": 0, "spend_7d": 0.0, "spend_30d": 0.0, "spend_90d": 0.0,
        "spend_365d": 1.0, "days_since_last_order": 365, "views_30d": 0, "carts_30d": 0, "events_30d": 0,
        "events_prev_30d": 0, "sessions_30d": 0, "days_since_last_event": 91,
    },
    "country and opt-in never seen in training": {
        **SAMPLE_CUSTOMER, "country": "ZZ", "marketing_opt_in": "unknown",
    },
}  # fmt: skip


@pytest.mark.parametrize("case", EDGE_CASES, ids=list(EDGE_CASES))
def test_edge_case_inputs_give_valid_probabilities(model, case):
    frame = to_frame([CustomerFeatures(**EDGE_CASES[case])])
    scores = proba(model, frame)
    assert np.isfinite(scores).all() and ((scores >= 0) & (scores <= 1)).all()


def test_no_activity_customer_is_high_risk(model):
    score = proba(
        model, to_frame([CustomerFeatures(**EDGE_CASES["one order a year ago, no activity since"])])
    )[0]
    assert score >= 0.8
