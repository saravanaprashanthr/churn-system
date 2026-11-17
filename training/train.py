"""One command to retrain: ``python -m training.train``.

Runs the tracked iterations on train -> valid, picks the best ranking model, calibrates it on valid,
scores the untouched test split once, chooses the decision threshold from the cost assumptions, writes
error analysis and feature importance, and registers the model in MLflow with alias ``candidate``.
Promotion to production happens only through the gate in Part F.

Environment: DATABASE_URL (published datasets), MLFLOW_TRACKING_URI (default http://localhost:5001).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import optuna
import pandas as pd
import psycopg
from mlflow import MlflowClient
from mlflow.models import infer_signature
from sklearn.inspection import permutation_importance

from training import models
from training.config import TrainConfig
from training.data import LABEL, Dataset, load_dataset, split, xy
from training.metrics import (
    best_threshold,
    breakeven_probability,
    campaign_profit,
    classification_metrics,
    segment_metrics,
)

logger = logging.getLogger("training")
TRAINING_DIR = Path(__file__).resolve().parent
CALIBRATED_RUN = "6_calibrated"
# Calibrate only if held-out Brier improves by at least this much; smaller gains are noise.
MIN_BRIER_GAIN = 0.001
REPO_ROOT = TRAINING_DIR.parent


@dataclass
class IterationResult:
    name: str
    run_id: str
    model_uri: str
    model: Any
    params: dict[str, Any]
    valid: dict[str, float]
    test: dict[str, float] = field(default_factory=dict)


def git_sha() -> str:
    def run(*args: str) -> str:
        # Fixed git subcommands to read the commit SHA; no user input reaches the command.
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip

    try:
        sha = run("rev-parse", "--short", "HEAD").strip()
        dirty = bool(run("status", "--porcelain", "--untracked-files=no").strip())
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get(
            "GIT_SHA", "unknown"
        )  # inside an image there is no .git; the build sets GIT_SHA
    return f"{sha}-dirty" if dirty else sha


def value_if_saved(frame: pd.DataFrame, config: TrainConfig) -> np.ndarray:
    """Gross margin of the spend a kept customer would bring over the horizon (their last-year run rate)."""
    horizon_share = config.split.horizon_days / 365.0
    return (config.cost.gross_margin * frame["spend_365d"] * horizon_share).to_numpy()


def segment_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["orders_365d_bucket"] = pd.cut(out["orders_365d"], [0, 1, 3, np.inf], labels=["1", "2-3", "4+"])
    out["recency_bucket"] = pd.cut(
        out["days_since_last_order"], [0, 30, 90, 180, 365], labels=["<=30d", "31-90d", "91-180d", "181-365d"]
    )
    out["tenure_bucket"] = pd.cut(out["tenure_days"], [0, 365, 730, np.inf], labels=["<1y", "1-2y", "2y+"])
    out["browsed_30d"] = np.where(out["events_30d"] > 0, "yes", "no")
    return out


SEGMENTS = (
    "orders_365d_bucket",
    "recency_bucket",
    "tenure_bucket",
    "browsed_30d",
    "country",
    "marketing_opt_in",
)


class Trainer:
    def __init__(self, config: TrainConfig, dataset: Dataset, out_dir: Path) -> None:
        self.config = config
        self.dataset = dataset
        self.out_dir = out_dir
        self.parts = split(dataset, config.split)
        self.X, self.y = {}, {}
        for name, part in self.parts.items():
            self.X[name], self.y[name] = xy(part, dataset.feature_columns)
        self.sha = git_sha()
        self.base_tags = {
            "git_sha": self.sha,
            "data_version": dataset.data_version,
            "snapshot_date": str(config.snapshot_date),
            "split_train": f"{config.split.train[0]}..{config.split.train[1]}",
            "split_valid": f"{config.split.valid[0]}..{config.split.valid[1]}",
            "split_test": f"{config.split.test[0]}..{config.split.test[1]}",
        }

    # ------------------------------------------------------------------ helpers
    def _metrics(self, model, split_name: str) -> dict[str, float]:
        scores = models.churn_probability(model, self.X[split_name])
        return classification_metrics(self.y[split_name], scores, self.config.top_k_fractions)

    def _start_run(self, name: str, description: str):
        return mlflow.start_run(
            run_name=name, tags={**self.base_tags, "iteration": name}, description=description
        )

    def _log_common(self) -> None:
        mlflow.log_params(
            {
                "rows_train": len(self.X["train"]),
                "rows_valid": len(self.X["valid"]),
                "rows_test": len(self.X["test"]),
                "n_features": len(self.dataset.feature_columns),
                "seed": self.config.seed,
            }
        )
        mlflow.log_dict({"dataset_versions": list(self.dataset.versions)}, "data/dataset_versions.json")

    def log_iteration(self, name: str, description: str, model, params: dict[str, Any]) -> IterationResult:
        valid = self._metrics(model, "valid")
        with self._start_run(name, description) as run:
            self._log_common()
            mlflow.log_params({f"model_{k}": v for k, v in params.items()})
            mlflow.log_metrics({f"valid_{k}": v for k, v in valid.items()})
            example = self.X["valid"].head(5)
            signature = infer_signature(example, models.churn_probability(model, example))
            info = mlflow.sklearn.log_model(
                model,
                name="model",
                signature=signature,
                input_example=example,
                code_paths=[str(TRAINING_DIR)],
                skops_trusted_types=list(models.SKOPS_TRUSTED_TYPES),
            )
        logger.info("%-26s valid pr_auc=%.4f roc_auc=%.4f brier=%.4f", name, valid["pr_auc"],
                    valid["roc_auc"], valid["brier"])  # fmt: skip
        return IterationResult(name, run.info.run_id, info.model_uri, model, params, valid)

    # ------------------------------------------------------------------ iterations
    def heuristic(self) -> dict[str, float]:
        """Reference point, not a model: rank customers by days since their last order."""
        # Scaled to [0, 1] only so the ranking metrics accept it; it is not a probability, so no Brier score.
        scores = self.X["valid"]["days_since_last_order"].to_numpy(dtype=float) / 366.0
        valid = classification_metrics(self.y["valid"], scores, self.config.top_k_fractions)
        valid.pop("brier")
        with self._start_run("heuristic_recency", "Score = days_since_last_order. No model, no fitting."):
            self._log_common()
            mlflow.log_metrics({f"valid_{k}": v for k, v in valid.items()})
        logger.info(
            "%-26s valid pr_auc=%.4f roc_auc=%.4f", "heuristic_recency", valid["pr_auc"], valid["roc_auc"]
        )
        return valid

    def fit(self, model):
        return model.fit(self.X["train"], self.y["train"])

    def tune(self, derived: bool) -> tuple[dict[str, Any], optuna.Study]:
        def objective(trial: optuna.Trial) -> float:
            model = self.fit(models.gradient_boosting(models.hgb_search_space(trial), derived=derived))
            return self._metrics(model, "valid")["pr_auc"]

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(
            direction="maximize", sampler=optuna.samplers.TPESampler(seed=self.config.seed)
        )
        study.optimize(objective, n_trials=self.config.optuna_trials)
        return study.best_params, study

    def choose_calibration(self, ranker: IterationResult) -> str | None:
        """Honest calibration check: fit on the first validation weeks, measure Brier on the later ones.

        Returns the method to use ("sigmoid" / "isotonic"), or None if neither beats the raw scores.
        The decision is logged as its own MLflow run.
        """
        cutoffs = sorted(self.parts["valid"]["cutoff_date"].unique())
        fit_weeks = set(cutoffs[: len(cutoffs) // 2 + 1])
        in_fit = self.parts["valid"]["cutoff_date"].isin(fit_weeks).to_numpy()
        X_fit, y_fit = self.X["valid"][in_fit], self.y["valid"][in_fit]
        X_eval, y_eval = self.X["valid"][~in_fit], self.y["valid"][~in_fit]
        brier = {
            "none": classification_metrics(y_eval, models.churn_probability(ranker.model, X_eval))["brier"]
        }
        for method in ("sigmoid", "isotonic"):
            calibrated = models.calibrate(ranker.model, X_fit, y_fit, method)
            brier[method] = classification_metrics(y_eval, models.churn_probability(calibrated, X_eval))[
                "brier"
            ]
        best = min(brier, key=lambda method: brier[method])
        if brier["none"] - brier[best] < MIN_BRIER_GAIN:
            best = "none"
        with self._start_run(
            "calibration_check",
            f"Calibration of {ranker.name}: fit on {len(fit_weeks)} "
            f"validation weeks, Brier on the {len(cutoffs) - len(fit_weeks)} later ones.",
        ):
            mlflow.log_metrics({f"heldout_brier_{k}": v for k, v in brier.items()})
            mlflow.log_params({"ranker": ranker.name, "chosen": best})
        logger.info("calibration check on %s: held-out Brier %s -> %s", ranker.name,
                    {k: round(v, 4) for k, v in brier.items()}, best)  # fmt: skip
        return None if best == "none" else best

    def run_iterations(self) -> tuple[list[IterationResult], dict[str, float]]:
        heuristic = self.heuristic()
        results = [
            self.log_iteration(
                "0_baseline_logreg",
                "Baseline: standardised features + one-hot categoricals, logistic regression.",
                self.fit(models.logistic_regression()),
                {"algorithm": "logistic_regression", "derived_features": False},
            ),
            self.log_iteration(
                "1_gradient_boosting",
                "Alternative algorithm: HistGradientBoosting with default parameters.",
                self.fit(models.gradient_boosting()),
                {
                    "algorithm": "hist_gradient_boosting",
                    "derived_features": False,
                    **models.DEFAULT_HGB_PARAMS,
                },
            ),
        ]
        derived = self.log_iteration(
            "2_gb_derived_features",
            "Feature work: recency relative to the customer's usual order gap, recent spend share, "
            "single-order flag, log spend.",
            self.fit(models.gradient_boosting(derived=True)),
            {"algorithm": "hist_gradient_boosting", "derived_features": True, **models.DEFAULT_HGB_PARAMS},
        )
        results.append(derived)
        use_derived = derived.valid["pr_auc"] > results[1].valid["pr_auc"]
        logger.info("derived features %s", "kept" if use_derived else "dropped (no validation gain)")

        best_params, study = self.tune(use_derived)
        tuned = self.log_iteration(
            "3_gb_tuned",
            f"Hyperparameter search: Optuna TPE, {self.config.optuna_trials} trials, "
            "objective = valid PR-AUC.",
            self.fit(models.gradient_boosting(best_params, derived=use_derived)),
            {"algorithm": "hist_gradient_boosting", "derived_features": use_derived, **best_params},
        )
        with mlflow.start_run(run_id=tuned.run_id):
            mlflow.log_text(study.trials_dataframe().to_csv(index=False), "optuna/trials.csv")
        results.append(tuned)

        linear = self.log_iteration(
            "4_logreg_log_derived",
            "Feature work for the linear model: derived features plus log1p on the heavy-tailed counts and "
            "spend (spend_365d mean $1,852 vs median $374), which raw standard scaling handles badly.",
            self.fit(models.logistic_regression(derived=True, log=True)),
            {"algorithm": "logistic_regression", "derived_features": True, "log_features": True},
        )
        results.append(linear)

        best_linear = max((r for r in results if r.params["algorithm"] == "logistic_regression"),
                          key=lambda r: r.valid["pr_auc"])  # fmt: skip
        linear_member = models.logistic_regression(
            derived=best_linear.params.get("derived_features", False),
            log=best_linear.params.get("log_features", False),
        )
        tree_member = models.gradient_boosting(best_params, derived=use_derived)
        ensemble = self.log_iteration(
            "5_ensemble",
            f"Soft vote (mean probability) of {best_linear.name} and {tuned.name}: close on PR-AUC but the "
            "tree model ranks better overall (ROC-AUC), so their errors differ.",
            self.fit(models.soft_vote(("logreg", linear_member), ("gb", tree_member))),
            {"algorithm": "soft_vote", "members": f"{best_linear.name}+{tuned.name}",
             "derived_features": use_derived},
        )  # fmt: skip
        results.append(ensemble)

        best_ranker = max(results, key=lambda r: r.valid["pr_auc"])
        method = self.choose_calibration(best_ranker)
        if method is None:
            logger.info("final model: %s, uncalibrated (calibration did not improve held-out Brier)",
                        best_ranker.name)  # fmt: skip
            return results, heuristic
        calibrated = self.log_iteration(
            CALIBRATED_RUN,
            f"{method} calibration of {best_ranker.name}, fitted on the whole validation split (model not "
            "refit). Kept because it improved Brier on held-out validation weeks; validation metrics of this "
            "run are in-sample for the calibrator, so judge it on test.",
            models.calibrate(best_ranker.model, self.X["valid"], self.y["valid"], method),
            {**best_ranker.params, "calibration": method, "calibrated_from": best_ranker.name},
        )
        results.append(calibrated)
        return results, heuristic

    # ------------------------------------------------------------------ final evaluation
    def evaluate_final(self, final: IterationResult, references: list[IterationResult]) -> dict[str, Any]:
        """Score the test split once; threshold from validation; error analysis; importance; register."""
        for result in [final, *references]:
            result.test = self._metrics(result.model, "test")

        valid_value = value_if_saved(self.parts["valid"], self.config)
        test_value = value_if_saved(self.parts["test"], self.config)
        valid_scores = models.churn_probability(final.model, self.X["valid"])
        test_scores = models.churn_probability(final.model, self.X["test"])
        chosen = best_threshold(self.y["valid"], valid_scores, valid_value, self.config.cost)
        threshold = chosen["threshold"]
        decision: dict[str, Any] = {
            "threshold": threshold,
            "chosen_on": "valid",
            "cost_assumptions": asdict(self.config.cost),
            "breakeven_probability_at_median_value": breakeven_probability(
                float(np.median(valid_value)), self.config.cost
            ),
            "valid": chosen,
            "test": campaign_profit(self.y["test"], test_scores, threshold, test_value, self.config.cost),
            "test_contact_everyone": campaign_profit(
                self.y["test"], test_scores, 0.0, test_value, self.config.cost
            ),
            "test_contact_nobody": campaign_profit(
                self.y["test"], test_scores, np.inf, test_value, self.config.cost
            ),
        }

        frame = segment_columns(self.parts["test"]).assign(score=test_scores)
        segments = pd.concat([segment_metrics(frame, LABEL, "score", s) for s in SEGMENTS], ignore_index=True)

        importance = permutation_importance(
            final.model, self.X["test"], self.y["test"], scoring="average_precision",
            n_repeats=10, random_state=self.config.seed,
        )  # fmt: skip
        importances = (
            pd.DataFrame(
                {
                    "feature": self.X["test"].columns,
                    "pr_auc_drop_mean": importance.importances_mean,
                    "pr_auc_drop_std": importance.importances_std,
                }
            )
            .sort_values("pr_auc_drop_mean", ascending=False)
            .reset_index(drop=True)
        )

        for result in [final, *references]:
            with mlflow.start_run(run_id=result.run_id):
                mlflow.log_metrics({f"test_{k}": v for k, v in result.test.items()})
        with mlflow.start_run(run_id=final.run_id):
            mlflow.log_dict(decision, "evaluation/decision_threshold.json")
            mlflow.log_text(segments.to_csv(index=False), "evaluation/metrics_by_segment.csv")
            mlflow.log_text(importances.to_csv(index=False), "evaluation/permutation_importance.csv")
            mlflow.log_metrics(
                {
                    "test_expected_profit": decision["test"]["expected_profit"],
                    "test_contacted_share": decision["test"]["contacted_share"],
                }
            )
            if np.isfinite(threshold):
                mlflow.log_metric("decision_threshold", threshold)
            else:  # no threshold pays off under the cost assumptions: contact nobody
                mlflow.set_tag("decision", "contact_nobody")
        return {"decision": decision, "segments": segments, "importances": importances}

    def register(self, final: IterationResult, decision: dict[str, Any]):
        client = MlflowClient()
        version = mlflow.register_model(final.model_uri, self.config.registered_model_name)
        name, number = self.config.registered_model_name, version.version
        tags = {
            "git_sha": self.sha,
            "data_version": self.dataset.data_version,
            "run_id": final.run_id,
            "iteration": final.name,
            "decision_threshold": f"{decision['threshold']:.6f}",
            "test_pr_auc": f"{final.test['pr_auc']:.6f}",
            "split_test": self.base_tags["split_test"],
        }
        for key, value in tags.items():
            client.set_model_version_tag(name, number, key, value)
        client.set_registered_model_alias(name, "candidate", number)
        logger.info("registered %s version %s with alias 'candidate'", name, number)
        return version


def select_final(results: list[IterationResult]) -> tuple[IterationResult, list[IterationResult]]:
    """Final model and the reference models also scored on test.

    Final = the calibrated model if the calibration check kept one, otherwise the best validation PR-AUC.
    References = the baseline and, for a calibrated final, the uncalibrated model it came from.
    """
    calibrated = [r for r in results if r.name == CALIBRATED_RUN]
    final = calibrated[0] if calibrated else max(results, key=lambda r: r.valid["pr_auc"])
    keep = {"0_baseline_logreg", final.params.get("calibrated_from")}
    return final, [r for r in results if r is not final and r.name in keep]


def write_reports(out_dir: Path, results: list[IterationResult], heuristic: dict, final: IterationResult,
                  evaluation: dict, trainer: Trainer, version) -> None:  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "data_version": trainer.dataset.data_version,
        "git_sha": trainer.sha,
        "rows": {k: len(v) for k, v in trainer.X.items()},
        "churn_rate": {k: float(v.mean()) for k, v in trainer.y.items()},
        "heuristic_recency_valid": heuristic,
        "iterations": [
            {"name": r.name, "run_id": r.run_id, "params": r.params, "valid": r.valid, "test": r.test}
            for r in results
        ],
        "final": final.name,
        "registered": {"name": version.name, "version": version.version, "alias": "candidate"},
        "decision": evaluation["decision"],
    }
    (out_dir / "training_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    evaluation["segments"].to_csv(out_dir / "metrics_by_segment.csv", index=False)
    evaluation["importances"].to_csv(out_dir / "permutation_importance.csv", index=False)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--optuna-trials", type=int, default=TrainConfig().optuna_trials)
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/training"))
    parser.add_argument("--experiment", default=TrainConfig().experiment_name)
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return 2
    config = replace(TrainConfig(), optuna_trials=args.optuna_trials, experiment_name=args.experiment)
    np.random.seed(config.seed)
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"))
    mlflow.set_experiment(config.experiment_name)

    with psycopg.connect(database_url) as conn:
        dataset = load_dataset(conn, config.snapshot_date, config.lookback_days, config.split.horizon_days)
    trainer = Trainer(config, dataset, args.out_dir)
    results, heuristic = trainer.run_iterations()
    final, references = select_final(results)
    evaluation = trainer.evaluate_final(final, references)
    version = trainer.register(final, evaluation["decision"])
    write_reports(args.out_dir, results, heuristic, final, evaluation, trainer, version)
    baseline = results[0]
    logger.info("test pr_auc=%.4f (baseline %.4f); threshold=%.4f; registered version %s",
                final.test["pr_auc"], baseline.test["pr_auc"], evaluation["decision"]["threshold"],
                version.version)  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
