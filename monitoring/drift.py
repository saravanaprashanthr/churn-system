"""Data and prediction drift: logged live requests compared with the training distribution.

    python -m monitoring.drift --since-hours 24

Reference = the production model's training rows (train split of training/config.py) and the model's scores
on them. Current = rows in ml_prediction_log from the last ``--since-hours``. Per feature: PSI (10 reference
quantile bins; categories for categoricals) and the KS statistic. Prediction drift: PSI of the scores.

Thresholds (conventional for PSI): < 0.10 stable, 0.10-0.25 warn, >= 0.25 alert. The run alerts when the
prediction PSI >= 0.25 (the scores the business acts on moved) or >= 3 features alert (one drifting feature
alone often does not hurt the model). Fewer than MIN_ROWS logged rows is "insufficient data", not "stable".

Exit codes: 0 stable/warn, 1 drift alert, 2 error, 3 insufficient data. Writes drift_report.{json,md} and a
Prometheus textfile (drift.prom) with churn_drift_* gauges.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

logger = logging.getLogger("monitoring.drift")

PSI_WARN, PSI_ALERT = 0.10, 0.25
FEATURE_ALERTS_FOR_ALERT = 3
MIN_ROWS = 200
EPSILON = 1e-4
CATEGORICAL = ("country", "marketing_opt_in")
SCORE_BINS = np.linspace(0.0, 1.0, 11)

EXIT_OK, EXIT_ALERT, EXIT_ERROR, EXIT_INSUFFICIENT = 0, 1, 2, 3


def status_of(psi: float) -> str:
    return "alert" if psi >= PSI_ALERT else "warn" if psi >= PSI_WARN else "stable"


def _psi_from_counts(expected: np.ndarray, actual: np.ndarray) -> float:
    e = np.clip(expected / max(expected.sum(), 1), EPSILON, None)
    a = np.clip(actual / max(actual.sum(), 1), EPSILON, None)
    return float(np.sum((a - e) * np.log(a / e)))


def numeric_psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """PSI over reference quantile bins (open-ended first and last bin)."""
    reference, current = np.asarray(reference, dtype=float), np.asarray(current, dtype=float)
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)[1:-1]))
    expected = np.bincount(np.searchsorted(edges, reference, side="right"), minlength=len(edges) + 1)
    actual = np.bincount(np.searchsorted(edges, current, side="right"), minlength=len(edges) + 1)
    return _psi_from_counts(expected.astype(float), actual.astype(float))


def categorical_psi(reference: pd.Series, current: pd.Series) -> float:
    categories = sorted(set(reference.astype(str)) | set(current.astype(str)))
    expected = reference.astype(str).value_counts().reindex(categories, fill_value=0).to_numpy(dtype=float)
    actual = current.astype(str).value_counts().reindex(categories, fill_value=0).to_numpy(dtype=float)
    return _psi_from_counts(expected, actual)


def score_psi(reference: np.ndarray, current: np.ndarray) -> float:
    expected, _ = np.histogram(np.clip(reference, 0, 1), bins=SCORE_BINS)
    actual, _ = np.histogram(np.clip(current, 0, 1), bins=SCORE_BINS)
    return _psi_from_counts(expected.astype(float), actual.astype(float))


@dataclass
class DriftReport:
    reference_rows: int
    current_rows: int
    window: str
    model_version: str | None
    features: dict[str, dict[str, float | str]] = field(default_factory=dict)
    prediction: dict[str, float | str] = field(default_factory=dict)
    status: str = "stable"
    reasons: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


def compare(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    reference_scores: np.ndarray,
    current_scores: np.ndarray,
    window: str = "",
    model_version: str | None = None,
) -> DriftReport:
    report = DriftReport(len(reference), len(current), window, model_version)
    if len(current) < MIN_ROWS:
        report.status = "insufficient_data"
        report.reasons.append(f"only {len(current)} logged rows, need {MIN_ROWS}")
        return report
    for column in reference.columns:
        if column in CATEGORICAL:
            psi, ks = categorical_psi(reference[column], current[column]), float("nan")
        else:
            psi = numeric_psi(reference[column].to_numpy(), current[column].to_numpy())
            ks = float(
                ks_2samp(
                    reference[column].to_numpy(dtype=float), current[column].to_numpy(dtype=float)
                ).statistic
            )
        report.features[column] = {"psi": round(psi, 4), "ks": round(ks, 4), "status": status_of(psi)}
    pred_psi = score_psi(reference_scores, current_scores)
    report.prediction = {
        "psi": round(pred_psi, 4),
        "status": status_of(pred_psi),
        "reference_mean": round(float(np.mean(reference_scores)), 4),
        "current_mean": round(float(np.mean(current_scores)), 4),
    }
    alerting = sorted(c for c, f in report.features.items() if f["status"] == "alert")
    if pred_psi >= PSI_ALERT:
        report.reasons.append(f"prediction PSI {pred_psi:.3f} >= {PSI_ALERT}")
    if len(alerting) >= FEATURE_ALERTS_FOR_ALERT:
        report.reasons.append(f"{len(alerting)} features with PSI >= {PSI_ALERT}: {', '.join(alerting)}")
    if report.reasons:
        report.status = "alert"
    elif pred_psi >= PSI_WARN or any(f["status"] != "stable" for f in report.features.values()):
        report.status = "warn"
    return report


def prometheus_text(report: DriftReport) -> str:
    lines = ["# TYPE churn_drift_feature_psi gauge"]
    lines += [f'churn_drift_feature_psi{{feature="{c}"}} {f["psi"]}' for c, f in report.features.items()]
    lines += [
        "# TYPE churn_drift_prediction_psi gauge",
        f"churn_drift_prediction_psi {report.prediction.get('psi', 0)}",
    ]
    lines += ["# TYPE churn_drift_alert gauge", f"churn_drift_alert {int(report.status == 'alert')}"]
    lines += ["# TYPE churn_drift_logged_rows gauge", f"churn_drift_logged_rows {report.current_rows}"]
    return "\n".join(lines) + "\n"


def render_markdown(report: DriftReport) -> str:
    lines = [
        f"# Drift report ({report.generated_at})",
        "",
        f"- **Status: {report.status.upper()}**"
        + (f": {'; '.join(report.reasons)}" if report.reasons else ""),
        f"- window: {report.window} · logged rows: {report.current_rows}"
        f" · reference rows: {report.reference_rows}"
        f" · model version: {report.model_version}",
    ]
    if report.prediction:
        p = report.prediction
        lines.append(
            f"- prediction PSI {p['psi']} ({p['status']}); "
            f"mean score {p['reference_mean']} -> {p['current_mean']}"
        )
    if report.features:
        lines += ["", "| Feature | PSI | KS | Status |", "|---|---:|---:|---|"]
        ordered = sorted(report.features.items(), key=lambda item: -float(item[1]["psi"]))
        lines += [f"| `{c}` | {f['psi']} | {f['ks']} | {f['status']} |" for c, f in ordered]
    return "\n".join(lines) + "\n"


def load_current(conn, since_hours: float) -> tuple[pd.DataFrame, np.ndarray, str | None]:
    rows = conn.execute(
        "SELECT features, churn_probability, model_version FROM ml_prediction_log "
        "WHERE logged_at >= now() - make_interval(secs => %s) ORDER BY id",
        (since_hours * 3600,),
    ).fetchall()
    if not rows:
        return pd.DataFrame(), np.array([]), None
    versions = pd.Series([r[2] for r in rows])
    return pd.DataFrame([r[0] for r in rows]), np.array([r[1] for r in rows], dtype=float), versions.mode()[0]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/monitoring"))
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return EXIT_ERROR

    import mlflow
    import psycopg

    from training.config import TrainConfig
    from training.data import load_dataset, split, xy

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"))
    config = TrainConfig()
    with psycopg.connect(database_url) as conn:
        current, current_scores, version = load_current(conn, args.since_hours)
        dataset = load_dataset(conn, config.snapshot_date, config.lookback_days, config.split.horizon_days)
    reference, _ = xy(split(dataset, config.split)["train"], dataset.feature_columns)
    window = f"last {args.since_hours:g} h"
    if version is None:
        report = compare(reference, current, np.array([]), current_scores, window)
    else:
        model = mlflow.sklearn.load_model(f"models:/{config.registered_model_name}/{version}")
        reference_scores = model.predict_proba(reference)[:, 1]
        report = compare(
            reference, current[list(reference.columns)], reference_scores, current_scores, window, version
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "drift_report.json").write_text(json.dumps(asdict(report), indent=2))
    (args.out_dir / "drift_report.md").write_text(render_markdown(report))
    (args.out_dir / "drift.prom").write_text(prometheus_text(report))
    logger.info("drift status: %s %s", report.status.upper(), "; ".join(report.reasons))
    if report.status == "alert":
        logger.warning("ALERT ChurnDataDrift: %s", "; ".join(report.reasons))
        return EXIT_ALERT
    return EXIT_INSUFFICIENT if report.status == "insufficient_data" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
