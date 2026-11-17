"""Promotion gate: the only way a model becomes ``production``.

    python -m training.promote --candidate models:/churn-90d/3

A candidate is promoted only if all of these hold:
  1. the model behavior tests (tests/model) pass for it: performance floor, directional, invariance and
     edge-case checks; skipped tests count as a failure;
  2. its training and validation labels close before the holdout starts (the holdout is unseen);
  3. its holdout PR-AUC is >= MIN_PR_AUC and, when a production model exists, >= production + MARGIN.
On acceptance: alias ``production`` moves to the candidate and ``previous-production`` to the old one, so a
rollback is one alias move. Every decision is written to artifacts/promotion/ and tagged on the version.
Exit codes: 0 promoted (or already production), 1 rejected, 2 error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import mlflow
import psycopg
from mlflow import MlflowClient
from sklearn.metrics import average_precision_score, roc_auc_score

from training.config import TrainConfig
from training.data import load_dataset, split, xy

logger = logging.getLogger("promote")
REPO_ROOT = Path(__file__).resolve().parents[1]

MARGIN = 0.005  # ~ the noise between models on one 13-week holdout (experiment_log.md)
MIN_PR_AUC = 0.85
PRODUCTION, PREVIOUS = "production", "previous-production"

EXIT_PROMOTED, EXIT_REJECTED, EXIT_ERROR = 0, 1, 2


@dataclass
class GateReport:
    model: str
    candidate_version: str
    production_version: str | None
    holdout: str
    margin: float = MARGIN
    min_pr_auc: float = MIN_PR_AUC
    candidate_metrics: dict[str, float] = field(default_factory=dict)
    production_metrics: dict[str, float] = field(default_factory=dict)
    behavior_tests: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    decision: str = "pending"
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


def resolve(client: MlflowClient, uri: str) -> tuple[str, str]:
    from serving.model import _parse_uri

    name, kind, ref = _parse_uri(uri)
    version = (
        client.get_model_version_by_alias(name, ref)
        if kind == "alias"
        else client.get_model_version(name, ref)
    )
    return name, str(version.version)


def production_version(client: MlflowClient, name: str) -> str | None:
    try:
        return str(client.get_model_version_by_alias(name, PRODUCTION).version)
    except mlflow.exceptions.MlflowException:
        return None


def run_behavior_tests(model_uri: str) -> dict[str, Any]:
    """Run tests/model against model_uri in a subprocess; passes only if every test ran and passed."""
    command = [sys.executable, "-m", "pytest", "tests/model", "-m", "model", "-q", "-rfEs", "--no-cov",
               "-p", "no:cacheprovider", "-W", "ignore"]  # fmt: skip
    result = subprocess.run(  # noqa: S603 - fixed command, our own test suite
        command, cwd=REPO_ROOT, env={**os.environ, "MODEL_URI": model_uri}, capture_output=True, text=True
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    summary = lines[-1] if lines else result.stderr.strip()[-300:]
    failures = [line for line in lines if line.startswith(("FAILED", "ERROR", "SKIPPED"))]
    passed = result.returncode == 0 and "skipped" not in summary and "passed" in summary
    return {"passed": passed, "summary": summary, "failures": failures[:20], "exit_code": result.returncode}


def labels_close_before(
    client: MlflowClient, name: str, version: str, holdout_start: date, horizon: int
) -> bool:
    """True if the candidate's validation labels (the latest data it saw) close before the holdout starts."""
    run_id = client.get_model_version(name, version).tags.get("run_id")
    if not run_id:
        return False
    split_valid = client.get_run(run_id).data.tags.get("split_valid", "")
    try:
        valid_end = date.fromisoformat(split_valid.split("..")[1])
    except (IndexError, ValueError):
        return False
    return valid_end + timedelta(days=horizon) <= holdout_start


def holdout_metrics(model_uri: str, X, y) -> dict[str, float]:
    scores = mlflow.sklearn.load_model(model_uri).predict_proba(X)[:, 1]
    return {"pr_auc": float(average_precision_score(y, scores)), "roc_auc": float(roc_auc_score(y, scores))}


def decide(report: GateReport) -> GateReport:
    cand = report.candidate_metrics.get("pr_auc", float("nan"))
    report.checks["behavior_tests_pass"] = bool(report.behavior_tests.get("passed"))
    report.checks["above_floor"] = cand >= report.min_pr_auc
    if report.production_version is not None:
        prod = report.production_metrics["pr_auc"]
        report.checks["beats_production_by_margin"] = cand >= prod + report.margin
    if not report.checks["behavior_tests_pass"]:
        report.reasons.append(f"behavior tests failed: {report.behavior_tests.get('summary')}")
    if not report.checks.get("holdout_unseen", False):
        report.reasons.append("candidate was trained on data overlapping the holdout")
    if not report.checks["above_floor"]:
        report.reasons.append(f"holdout PR-AUC {cand:.4f} < floor {report.min_pr_auc}")
    if report.checks.get("beats_production_by_margin") is False:
        report.reasons.append(
            f"holdout PR-AUC {cand:.4f} < production v{report.production_version} "
            f"{report.production_metrics['pr_auc']:.4f} + margin {report.margin}"
        )
    report.decision = "rejected" if report.reasons else "promoted"
    return report


def write_report(report: GateReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.checked_at.replace(":", "").replace("-", "")
    path = out_dir / f"{stamp}_{report.model}_v{report.candidate_version}_{report.decision}.json"
    path.write_text(json.dumps(asdict(report), indent=2))
    return path


def gate(candidate_uri: str, config: TrainConfig, database_url: str, out_dir: Path) -> GateReport:
    client = MlflowClient()
    name, candidate = resolve(client, candidate_uri)
    production = production_version(client, name)
    holdout_start, holdout_end = config.split.test
    report = GateReport(name, candidate, production, f"{holdout_start}..{holdout_end}")
    logger.info(
        "gate: %s v%s (production: %s), holdout %s", name, candidate, production or "none", report.holdout
    )
    if production == candidate:
        report.decision = "already-production"
        return report

    with psycopg.connect(database_url) as conn:
        dataset = load_dataset(conn, config.snapshot_date, config.lookback_days, config.split.horizon_days)
    X, y = xy(split(dataset, config.split)["test"], dataset.feature_columns)

    report.behavior_tests = run_behavior_tests(f"models:/{name}/{candidate}")
    logger.info("behavior tests: %s", report.behavior_tests["summary"])
    for failure in report.behavior_tests["failures"]:
        logger.info("  %s", failure)
    report.checks["holdout_unseen"] = labels_close_before(
        client, name, candidate, holdout_start, config.split.horizon_days
    )
    report.candidate_metrics = holdout_metrics(f"models:/{name}/{candidate}", X, y)
    if production is not None:
        report.production_metrics = holdout_metrics(f"models:/{name}/{production}", X, y)
    decide(report)

    for key, value in {"gate_decision": report.decision, "gate_checked_at": report.checked_at,
                       "gate_holdout_pr_auc": f"{report.candidate_metrics['pr_auc']:.6f}",
                       "gate_compared_to": production or "none"}.items():  # fmt: skip
        client.set_model_version_tag(name, candidate, key, value)
    if report.decision == "promoted":
        if production is not None:
            client.set_registered_model_alias(name, PREVIOUS, production)
        client.set_registered_model_alias(name, PRODUCTION, candidate)
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--candidate", default="models:/churn-90d@candidate")
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/promotion"))
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return EXIT_ERROR
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"))
    try:
        report = gate(args.candidate, TrainConfig(), database_url, args.out_dir)
    except (mlflow.exceptions.MlflowException, psycopg.Error, ValueError) as exc:
        logger.error("gate could not run: %s", exc)
        return EXIT_ERROR
    path = write_report(report, args.out_dir)
    logger.info("decision: %s v%s -> %s", report.model, report.candidate_version, report.decision.upper())
    for reason in report.reasons:
        logger.info("  reason: %s", reason)
    if report.candidate_metrics:
        logger.info("  candidate holdout PR-AUC %.4f; production %s", report.candidate_metrics["pr_auc"],
                    f"v{report.production_version} {report.production_metrics['pr_auc']:.4f}"
                    if report.production_metrics else "none")  # fmt: skip
    logger.info("report: %s", path)
    return EXIT_REJECTED if report.decision == "rejected" else EXIT_PROMOTED


if __name__ == "__main__":
    sys.exit(main())
