"""Metric definitions on small hand-checked examples."""

import numpy as np
import pandas as pd
import pytest

from training.config import CostConfig
from training.metrics import (
    best_threshold,
    breakeven_probability,
    campaign_profit,
    classification_metrics,
    lift_at_k,
    precision_at_k,
    recall_at_k,
    segment_metrics,
)

Y = np.array([1, 1, 0, 1, 0, 0, 0, 0, 1, 0])
SCORES = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05])
COST = CostConfig(contact_cost=5.0, save_rate=0.2, gross_margin=0.3)


def test_top_k_metrics_on_a_known_ranking():
    # top 20% = 2 rows (0.9, 0.8): both churners; 4 churners in total, base rate 0.4
    assert recall_at_k(Y, SCORES, 0.2) == pytest.approx(0.5)
    assert precision_at_k(Y, SCORES, 0.2) == pytest.approx(1.0)
    assert lift_at_k(Y, SCORES, 0.2) == pytest.approx(2.5)
    # top 50% = 5 rows: 3 churners
    assert recall_at_k(Y, SCORES, 0.5) == pytest.approx(0.75)


def test_perfect_ranking_has_pr_auc_one_and_random_lift_one():
    y = np.array([1, 1, 0, 0])
    assert classification_metrics(y, np.array([0.9, 0.8, 0.2, 0.1]), (0.5,))["pr_auc"] == pytest.approx(1.0)
    assert lift_at_k(y, np.array([0.5, 0.5, 0.5, 0.5]), 1.0) == pytest.approx(1.0)


def test_top_k_rejects_bad_fraction():
    with pytest.raises(ValueError):
        recall_at_k(Y, SCORES, 0.0)


def test_campaign_profit_counts_gains_only_on_contacted_churners():
    result = campaign_profit(Y, SCORES, threshold=0.75, value_if_saved=100.0, cost=COST)
    # contacted: 0.9 and 0.8 (both churners) -> 2 * 100 * 0.2 - 2 * 5 = 30
    assert result == {
        "threshold": 0.75, "contacted": 2, "contacted_share": 0.2, "churners_reached": 2,
        "expected_profit": 30.0,
    }  # fmt: skip


def test_best_threshold_stops_where_extra_contacts_lose_money():
    best = best_threshold(Y, SCORES, value_if_saved=100.0, cost=COST)
    # each churner reached earns 20, each contact costs 5: best is the top 4 (3 churners): 60 - 20 = 40
    assert best["threshold"] == pytest.approx(0.6)
    assert best["expected_profit"] == pytest.approx(40.0)


def test_best_threshold_contacts_nobody_when_offers_never_pay():
    best = best_threshold(Y, SCORES, value_if_saved=1.0, cost=COST)
    assert best["contacted"] == 0 and best["expected_profit"] == 0.0


def test_breakeven_probability():
    # contact pays off when p * 0.2 * 100 > 5, i.e. p > 0.25
    assert breakeven_probability(100.0, COST) == pytest.approx(0.25)


def test_segment_metrics_per_group_and_single_class_segments():
    frame = pd.DataFrame({"y": Y, "s": SCORES, "g": ["a"] * 5 + ["b"] * 4 + ["c"]})
    table = segment_metrics(frame, "y", "s", "g").set_index("segment")
    assert table.loc["g=a", "rows"] == 5
    assert table.loc["g=a", "churn_rate"] == pytest.approx(0.6)
    assert np.isnan(table.loc["g=c", "pr_auc"])  # one row, one class: AUC undefined, not an error
