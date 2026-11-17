"""Drift statistics, drift decisions, delayed-label metrics and the alert rules."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from monitoring import drift, performance
from serving.app import create_app
from serving.model import Settings
from tests.serving.conftest import customer, loaded

RNG = np.random.default_rng(0)
ROOT = Path(__file__).resolve().parents[2]


def frame(n: int, shift: float = 0.0, country: str = "US") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "days_since_last_order": RNG.integers(1, 365, n) + shift,
            "orders_365d": RNG.integers(1, 12, n) + shift / 30,
            "spend_365d": RNG.gamma(2.0, 200.0, n) * (1 + shift / 100),
            "country": [country] * n,
        }
    )


def test_psi_is_zero_for_the_same_distribution_and_large_for_a_shift():
    reference = RNG.normal(0, 1, 5000)
    assert drift.numeric_psi(reference, RNG.normal(0, 1, 5000)) < 0.02
    assert drift.numeric_psi(reference, RNG.normal(1.5, 1, 5000)) > 0.25


def test_categorical_and_score_psi():
    reference = pd.Series(["US"] * 500 + ["DE"] * 500)
    assert drift.categorical_psi(reference, reference.sample(frac=1, random_state=1)) < 1e-9
    assert drift.categorical_psi(reference, pd.Series(["US"] * 950 + ["DE"] * 50)) > 0.25
    assert drift.score_psi(RNG.uniform(0, 1, 2000), RNG.uniform(0, 0.3, 2000)) > 0.25


@pytest.mark.parametrize(
    ("value", "status"), [(0.05, "stable"), (0.1, "warn"), (0.24, "warn"), (0.25, "alert")]
)
def test_status_thresholds(value, status):
    assert drift.status_of(value) == status


def test_no_drift_is_stable():
    reference, current = frame(3000), frame(1000)
    scores = RNG.uniform(0, 1, 3000)
    report = drift.compare(reference, current, scores, RNG.uniform(0, 1, 1000))
    assert report.status in ("stable", "warn") and report.reasons == []


def test_shifted_inputs_and_scores_alert_with_reasons():
    reference, current = frame(3000), frame(1000, shift=150, country="DE")
    report = drift.compare(reference, current, RNG.uniform(0.4, 0.9, 3000), RNG.uniform(0.0, 0.3, 1000))
    assert report.status == "alert"
    assert any("prediction PSI" in r for r in report.reasons)
    assert any("features with PSI" in r for r in report.reasons)
    assert report.features["country"]["status"] == "alert"
    text = drift.prometheus_text(report)
    assert "churn_drift_alert 1" in text and 'churn_drift_feature_psi{feature="country"}' in text
    assert "Status: ALERT" in drift.render_markdown(report)


def test_too_few_rows_is_insufficient_data_not_stable():
    report = drift.compare(frame(3000), frame(50), RNG.uniform(0, 1, 3000), RNG.uniform(0, 1, 50))
    assert report.status == "insufficient_data"


def labelled(n: int, quality: float) -> pd.DataFrame:
    y = RNG.integers(0, 2, n)
    p = np.clip(quality * y + (1 - quality) * RNG.uniform(0, 1, n), 0, 1)
    return pd.DataFrame({"customer_id": range(n), "as_of_date": ["2026-01-05"] * n, "model_version": "3",
                         "churn_probability": p, "at_risk": p >= 0.5, "churned": y})  # fmt: skip


def test_realized_performance_ok_and_alert():
    good = performance.evaluate(labelled(400, quality=0.9))
    assert good["status"] == "ok" and good["by_model"]["3"]["pr_auc"] > 0.85
    bad = performance.evaluate(labelled(400, quality=0.0))
    assert bad["status"] == "alert" and "realized PR-AUC" in bad["reasons"][0]
    assert 'churn_realized_pr_auc{model_version="3"}' in performance.prometheus_text(bad)


def test_unmatured_predictions_are_pending_not_scored():
    rows = labelled(10, quality=0.9).assign(churned=None)
    report = performance.evaluate(rows)
    assert report == {**report, "status": "nothing_matured", "pending": 10, "matured": 0}


def test_every_metric_in_the_alert_rules_exists():
    """An alert on a metric nobody exports never fires; check every name against the real exporters."""
    rules = yaml.safe_load((ROOT / "monitoring" / "alerts.yaml").read_text())
    alerts = [rule for group in rules["groups"] for rule in group["rules"]]
    assert all({"alert", "expr", "labels", "annotations"} <= set(rule) for rule in alerts)
    assert {rule["labels"]["severity"] for rule in alerts} <= {"warning", "critical"}

    with TestClient(create_app(Settings(), loader=lambda s: loaded())) as client:
        client.post("/predict", json={"instances": [customer()]})
        exported = client.get("/metrics").text
    exported += drift.prometheus_text(drift.compare(frame(300), frame(300), RNG.uniform(0, 1, 300),
                                                    RNG.uniform(0, 1, 300)))  # fmt: skip
    exported += performance.prometheus_text(performance.evaluate(labelled(300, quality=0.9)))
    names = set(re.findall(r"^([a-z_]+)(?:\{|\s)", exported, flags=re.M))
    for rule in alerts:
        used = set(re.findall(r"\b(churn_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\[{\s)>=<]|$)", rule["expr"]))
        missing = {m for m in used if m not in names and f"{m}_bucket" not in names}
        assert not missing, f"{rule['alert']} uses metrics nobody exports: {missing}"
