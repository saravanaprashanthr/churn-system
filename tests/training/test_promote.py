"""Promotion decision rules, without MLflow or a database."""

from __future__ import annotations

import pytest

from training.promote import MARGIN, MIN_PR_AUC, GateReport, decide


def report(cand: float, prod: float | None = None, tests: bool = True, unseen: bool = True) -> GateReport:
    r = GateReport("churn-90d", "5", None if prod is None else "3", "2026-01-05..2026-03-30")
    r.behavior_tests = {"passed": tests, "summary": "13 passed" if tests else "1 failed, 12 passed"}
    r.checks["holdout_unseen"] = unseen
    r.candidate_metrics = {"pr_auc": cand}
    if prod is not None:
        r.production_metrics = {"pr_auc": prod}
    return decide(r)


def test_first_model_is_promoted_when_tests_pass_and_above_floor():
    r = report(0.89)
    assert r.decision == "promoted" and r.reasons == []


def test_better_by_at_least_the_margin_is_promoted():
    assert report(0.89 + MARGIN, prod=0.89).decision == "promoted"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"cand": 0.893, "prod": 0.890}, "+ margin"),  # better, but within noise
        ({"cand": 0.880, "prod": 0.890}, "+ margin"),  # worse
        ({"cand": MIN_PR_AUC - 0.01}, "floor"),
        ({"cand": 0.95, "tests": False}, "behavior tests failed"),  # great metrics do not excuse bad behavior
        ({"cand": 0.95, "unseen": False}, "overlapping the holdout"),
    ],
)
def test_rejections_name_their_reason(kwargs, reason):
    r = report(**kwargs)
    assert r.decision == "rejected"
    assert any(reason in text for text in r.reasons), r.reasons


def test_every_failed_check_is_reported():
    r = report(0.80, prod=0.89, tests=False, unseen=False)
    assert len(r.reasons) == 4
