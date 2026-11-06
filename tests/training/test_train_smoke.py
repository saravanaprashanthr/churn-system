"""End to end on synthetic data with a throwaway MLflow store: every iteration is tracked and registered."""

from __future__ import annotations

from dataclasses import replace

import mlflow
import pytest
from mlflow import MlflowClient

from tests.training.synthetic import synthetic_dataset
from training.config import TrainConfig
from training.train import Trainer, write_reports


@pytest.fixture
def tracking(tmp_path):
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    mlflow.set_experiment("smoke")
    yield
    mlflow.set_tracking_uri(previous)


def test_training_run_tracks_iterations_and_registers_candidate(tracking, tmp_path):
    config = replace(TrainConfig(), optuna_trials=2, registered_model_name="churn-smoke")
    trainer = Trainer(config, synthetic_dataset(rows_per_cutoff=60), tmp_path)
    results, heuristic = trainer.run_iterations()
    names = [r.name for r in results]
    assert names == ["0_baseline_logreg", "1_gradient_boosting", "2_gb_derived_features", "3_gb_tuned",
                     "4_calibrated"]  # fmt: skip

    final = results[-1]
    references = [results[0]]
    evaluation = trainer.evaluate_final(final, references)
    version = trainer.register(final, evaluation["decision"])
    write_reports(tmp_path / "out", results, heuristic, final, evaluation, trainer, version)

    client = MlflowClient()
    runs = mlflow.search_runs(experiment_names=["smoke"])
    assert len(runs) == 6  # heuristic + 5 iterations
    final_run = client.get_run(final.run_id)
    assert {"git_sha", "data_version", "split_test"} <= set(final_run.data.tags)
    assert "test_pr_auc" in final_run.data.metrics and "valid_pr_auc" in final_run.data.metrics
    assert final_run.data.metrics["decision_threshold"] == evaluation["decision"]["threshold"]
    assert (
        evaluation["decision"]["test"]["expected_profit"]
        > evaluation["decision"]["test_contact_nobody"]["expected_profit"]
    )

    registered = client.get_model_version_by_alias("churn-smoke", "candidate")
    assert registered.version == version.version
    assert registered.tags["data_version"] == trainer.dataset.data_version
    model = mlflow.pyfunc.load_model("models:/churn-smoke@candidate")
    assert model.metadata.signature is not None
    assert (tmp_path / "out" / "training_summary.json").exists()
    assert set(evaluation["segments"]["segment"].str.split("=").str[0]) >= {
        "orders_365d_bucket",
        "recency_bucket",
    }
