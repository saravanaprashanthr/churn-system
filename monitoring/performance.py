"""Performance with delayed labels: realized metrics once each prediction's 90-day outcome is known.

    python -m monitoring.performance --snapshot-date 2026-07-01

A prediction made for cutoff ``as_of_date`` gets its label only when ``as_of_date + 90 days <= snapshot``:
churned = no completed order in [as_of_date, as_of_date + 90). Until then it is counted as "pending".
Matured predictions are joined with orders and scored per model version and per cutoff week.

Alert: realized PR-AUC below the promotion floor (0.85) on at least MIN_ROWS matured predictions.
Exit codes: 0 ok, 1 alert, 2 error, 3 nothing matured yet. Writes performance_report.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

logger = logging.getLogger("monitoring.performance")

MIN_PR_AUC = 0.85
MIN_ROWS = 200
EXIT_OK, EXIT_ALERT, EXIT_ERROR, EXIT_NOTHING_MATURED = 0, 1, 2, 3

# Latest log row per (customer, cutoff, model version), labelled from orders once the window has closed.
MATURED_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (customer_id, as_of_date, model_version)
               customer_id, as_of_date, model_version, churn_probability, at_risk
        FROM ml_prediction_log
        WHERE as_of_date IS NOT NULL AND customer_id IS NOT NULL
        ORDER BY customer_id, as_of_date, model_version, id DESC
    )
    SELECT l.customer_id, l.as_of_date, l.model_version, l.churn_probability, l.at_risk,
           CASE WHEN l.as_of_date + %(horizon)s <= %(snapshot)s THEN
               (NOT EXISTS (SELECT 1 FROM orders o
                            WHERE o.customer_id = l.customer_id AND o.status = 'completed'
                              AND o.order_date >= l.as_of_date
                              AND o.order_date < l.as_of_date + %(horizon)s))::int
           END AS churned
    FROM latest l
"""


def summarize(frame: pd.DataFrame) -> dict[str, float | int]:
    y, p = frame["churned"].to_numpy(dtype=int), frame["churn_probability"].to_numpy(dtype=float)
    flagged = frame["at_risk"].to_numpy(dtype=bool)
    both = 0 < y.sum() < len(y)
    return {
        "rows": len(frame),
        "churn_rate": round(float(y.mean()), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4) if both else float("nan"),
        "roc_auc": round(float(roc_auc_score(y, p)), 4) if both else float("nan"),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "flagged": int(flagged.sum()),
        "precision_of_flagged": round(float(y[flagged].mean()), 4) if flagged.any() else float("nan"),
    }


def evaluate(rows: pd.DataFrame) -> dict:
    matured = rows[rows["churned"].notna()].copy()
    report: dict = {"pending": int(rows["churned"].isna().sum()), "matured": len(matured), "by_model": {},
                    "by_week": [], "status": "nothing_matured", "reasons": []}  # fmt: skip
    if matured.empty:
        return report
    matured["churned"] = matured["churned"].astype(int)
    for version, part in matured.groupby("model_version"):
        report["by_model"][str(version)] = summarize(part)
    for (cutoff, model_version), part in matured.groupby(["as_of_date", "model_version"]):
        report["by_week"].append(
            {"as_of_date": str(cutoff), "model_version": str(model_version), **summarize(part)}
        )
    report["status"] = "ok"
    for version, metrics in report["by_model"].items():
        if metrics["rows"] >= MIN_ROWS and not np.isnan(metrics["pr_auc"]) and metrics["pr_auc"] < MIN_PR_AUC:
            report["status"] = "alert"
            report["reasons"].append(f"model v{version}: realized PR-AUC {metrics['pr_auc']} < {MIN_PR_AUC}")
    return report


def prometheus_text(report: dict) -> str:
    lines = ["# TYPE churn_realized_pr_auc gauge", "# TYPE churn_realized_rows gauge"]
    for version, m in report["by_model"].items():
        if not np.isnan(m["pr_auc"]):
            lines.append(f'churn_realized_pr_auc{{model_version="{version}"}} {m["pr_auc"]}')
        lines.append(f'churn_realized_rows{{model_version="{version}"}} {m["rows"]}')
    lines += ["# TYPE churn_realized_pending gauge", f"churn_realized_pending {report['pending']}"]
    return "\n".join(lines) + "\n"


def render_markdown(report: dict, snapshot: date) -> str:
    lines = [
        f"# Realized performance (labels known up to {snapshot})",
        "",
        f"- **Status: {report['status'].upper()}**"
        + (f": {'; '.join(report['reasons'])}" if report["reasons"] else ""),
        f"- matured predictions: {report['matured']} · "
        f"pending (label window still open): {report['pending']}",
        "",
        "| Model | Rows | Churn rate | PR-AUC | ROC-AUC | Brier | Flagged | Precision of flagged |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for version, m in report["by_model"].items():
        lines.append(f"| v{version} | {m['rows']} | {m['churn_rate']} | {m['pr_auc']} | {m['roc_auc']} | "
                     f"{m['brier']} | {m['flagged']} | {m['precision_of_flagged']} |")  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot-date", type=date.fromisoformat, default=date(2026, 7, 1))
    parser.add_argument("--horizon-days", type=int, default=90)
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/monitoring"))
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return EXIT_ERROR

    import psycopg

    with psycopg.connect(database_url) as conn:
        cursor = conn.execute(MATURED_SQL, {"horizon": args.horizon_days, "snapshot": args.snapshot_date})
        columns = [c.name for c in cursor.description or []]
        rows = pd.DataFrame(cursor.fetchall(), columns=columns)
    if rows.empty:
        rows = pd.DataFrame(columns=["customer_id", "as_of_date", "model_version", "churn_probability",
                                     "at_risk", "churned"])  # fmt: skip
    report = evaluate(rows)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "performance_report.json").write_text(json.dumps(report, indent=2, default=str))
    (args.out_dir / "performance_report.md").write_text(render_markdown(report, args.snapshot_date))
    (args.out_dir / "performance.prom").write_text(prometheus_text(report))
    logger.info(
        "performance: %s; matured %d, pending %d", report["status"], report["matured"], report["pending"]
    )
    for version, metrics in report["by_model"].items():
        logger.info("  model v%s: %s", version, metrics)
    if report["status"] == "alert":
        logger.warning("ALERT ChurnRealizedPerformanceDrop: %s", "; ".join(report["reasons"]))
        return EXIT_ALERT
    return EXIT_NOTHING_MATURED if report["status"] == "nothing_matured" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
