"""Time split with embargo, model pipelines and derived features."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.training.synthetic import synthetic_dataset
from training import models
from training.config import SplitConfig
from training.data import split, xy


def test_default_split_has_embargo_between_label_windows():
    config = SplitConfig()
    assert config.train[1] + timedelta(days=90) <= config.valid[0]
    assert config.valid[1] + timedelta(days=90) <= config.test[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"train": (date(2025, 1, 6), date(2025, 7, 1))},  # train labels reach into valid
        {"valid": (date(2025, 8, 11), date(2025, 12, 1))},  # valid labels reach into test
        {"test": (date(2026, 3, 30), date(2026, 1, 5))},  # reversed
    ],
)
def test_overlapping_or_reversed_splits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        SplitConfig(**kwargs)


def test_split_assigns_by_cutoff_and_drops_embargo_weeks():
    dataset = synthetic_dataset()
    parts = split(dataset, SplitConfig())
    assert parts["train"]["cutoff_date"].nunique() == 19
    assert parts["valid"]["cutoff_date"].nunique() == 9
    assert parts["test"]["cutoff_date"].nunique() == 13
    assert parts["train"]["cutoff_date"].max() < parts["valid"]["cutoff_date"].min()
    assert parts["valid"]["cutoff_date"].max() < parts["test"]["cutoff_date"].min()
    used = sum(len(p) for p in parts.values())
    assert used < len(dataset.frame)  # embargo weeks are not used anywhere


def test_data_version_depends_only_on_the_set_of_versions():
    a = synthetic_dataset()
    b = synthetic_dataset()
    assert a.data_version == b.data_version
    shorter = synthetic_dataset(weeks=64)
    assert shorter.data_version != a.data_version


def test_derived_features():
    X = pd.DataFrame(
        {"orders_365d": [4, 1], "days_since_last_order": [182, 10], "spend_90d": [50.0, 0.0],
         "spend_365d": [200.0, 0.0]}
    )  # fmt: skip
    out = models.add_derived_features(X)
    assert out["recency_vs_gap"].tolist() == pytest.approx([182 / (365 / 4), 10 / 365])
    assert out["spend_share_90d"].tolist() == [0.25, 0.0]
    assert out["single_order"].tolist() == [0, 1]
    assert "recency_vs_gap" not in X  # input untouched


@pytest.fixture(scope="module")
def data():
    dataset = synthetic_dataset(rows_per_cutoff=60)
    parts = split(dataset, SplitConfig())
    return {name: xy(part, dataset.feature_columns) for name, part in parts.items()}


@pytest.mark.parametrize(
    "build",
    [models.logistic_regression, models.gradient_boosting, lambda: models.gradient_boosting(derived=True)],
    ids=["logreg", "hgb", "hgb_derived"],
)
def test_models_learn_the_synthetic_signal_and_handle_unseen_categories(data, build):
    X_train, y_train = data["train"]
    X_valid, y_valid = data["valid"]
    model = build().fit(X_train, y_train)
    scores = models.churn_probability(model, X_valid)
    assert scores.shape == (len(X_valid),)
    assert ((scores >= 0) & (scores <= 1)).all()
    assert np.corrcoef(scores, X_valid["days_since_last_order"])[0, 1] > 0.3
    unseen = X_valid.head(3).assign(country="ZZ", marketing_opt_in="maybe")
    assert np.isfinite(models.churn_probability(model, unseen)).all()


def test_calibration_keeps_the_model_frozen_and_returns_probabilities(data):
    X_train, y_train = data["train"]
    X_valid, y_valid = data["valid"]
    fitted = models.gradient_boosting().fit(X_train, y_train)
    before = models.churn_probability(fitted, X_valid)
    calibrated = models.calibrate(fitted, X_valid, y_valid)
    assert np.array_equal(models.churn_probability(fitted, X_valid), before)  # not refit
    scores = models.churn_probability(calibrated, X_valid)
    assert ((scores >= 0) & (scores <= 1)).all()
    assert abs(scores.mean() - y_valid.mean()) < 0.02  # isotonic matches the calibration base rate


def test_log_features_keep_order_and_leave_trends_untouched():
    X = pd.DataFrame({"spend_365d": [0.0, 100.0, 10_000.0], "order_trend_30d": [-2, 0, 3]})
    out = models.log_skewed_features(X)
    assert out["spend_365d"].is_monotonic_increasing
    assert out["spend_365d"].max() < 10
    assert out["order_trend_30d"].tolist() == [-2, 0, 3]


def test_every_saved_model_type_is_in_the_skops_trust_list(data):
    """MLflow saves with skops; a type missing from SKOPS_TRUSTED_TYPES makes log_model fail at the end."""
    import skops.io as sio

    X_train, y_train = data["train"]
    X_valid, y_valid = data["valid"]
    lr = models.logistic_regression(derived=True, log=True).fit(X_train, y_train)
    gb = models.gradient_boosting(derived=True).fit(X_train, y_train)
    candidates = [
        lr,
        gb,
        models.soft_vote(("logreg", models.logistic_regression()), ("gb", models.gradient_boosting())).fit(
            X_train, y_train
        ),
        models.calibrate(gb, X_valid, y_valid, "isotonic"),
        models.calibrate(lr, X_valid, y_valid, "sigmoid"),
    ]
    for model in candidates:
        untrusted = set(sio.get_untrusted_types(data=sio.dumps(model)))
        assert untrusted <= set(models.SKOPS_TRUSTED_TYPES), untrusted
