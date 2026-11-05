"""Validation rules and dataset versioning, without a database."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from pipeline.publish import DatasetParams, content_hash, dataset_version
from pipeline.validate import FeatureValidationError, ValidationParams, validate
from tests.pipeline.test_transform import CUTOFF, build

VALIDATION = ValidationParams(cutoff_date=CUTOFF, lookback_days=365, labeled=True, min_rows=1)
DATASET = DatasetParams(
    cutoff_date=CUTOFF, snapshot_date=pd.Timestamp("2026-07-01").date(), lookback_days=365, horizon_days=90,
    labeled=True,
)  # fmt: skip


def test_valid_features_pass_with_report():
    report = validate(build(), VALIDATION)
    assert report["passed"] is True
    assert report["rows"] == 3
    assert report["churn_rate"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda df: df.assign(orders_30d=-1), "orders_30d"),
        (lambda df: pd.concat([df, df.iloc[[0]]], ignore_index=True), "customer_id"),
        (lambda df: df.assign(churned=2), "churned"),
        (lambda df: df.assign(orders_7d=df["orders_365d"] + 1), "nested_windows"),
        (lambda df: df.assign(marketing_opt_in="maybe"), "marketing_opt_in"),
        (lambda df: df.assign(extra_column=1), "extra_column"),
        (lambda df: df.assign(cutoff_date=pd.Timestamp("2025-01-01").date()), "cutoff_date"),
    ],
    ids=["negative-count", "duplicate-customer", "bad-label", "windows-not-nested", "bad-category",
         "unexpected-column", "wrong-cutoff"],
)  # fmt: skip
def test_invalid_features_are_rejected(mutate, expected):
    with pytest.raises(FeatureValidationError, match=expected):
        validate(mutate(build()), VALIDATION)


def test_too_few_rows_is_rejected():
    with pytest.raises(FeatureValidationError, match="only 3 rows"):
        validate(build(), replace(VALIDATION, min_rows=50))


def test_degenerate_label_is_rejected():
    with pytest.raises(FeatureValidationError, match="degenerate"):
        validate(build().assign(churned=1), VALIDATION)


def test_dataset_version_is_deterministic_and_row_order_independent():
    features = build()
    version, full_hash = dataset_version(features, DATASET)
    assert version.startswith("20250707-") and len(version) == len("20250707-") + 12
    assert full_hash.startswith(version.split("-")[1])
    assert dataset_version(features.iloc[::-1], DATASET) == (version, full_hash)


def test_dataset_version_changes_with_values_and_parameters():
    features = build()
    base = content_hash(features, DATASET)
    assert content_hash(features.assign(spend_7d=features["spend_7d"] + 0.01), DATASET) != base
    assert content_hash(features, replace(DATASET, lookback_days=180)) != base
    assert content_hash(features, replace(DATASET, snapshot_date=pd.Timestamp("2026-08-01").date())) != base
