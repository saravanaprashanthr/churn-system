"""Registry -> bundled fallback -> not ready: the defined behavior when the registry is unreachable."""

from __future__ import annotations

import json
from pathlib import Path

import mlflow
import pytest
from sklearn.dummy import DummyClassifier

from serving.model import (
    METADATA_FILE,
    LoadedModel,
    ModelLoadError,
    Settings,
    _parse_uri,
    info_from_tags,
    load_bundled,
    load_model,
)
from tests.serving.conftest import loaded


def test_model_uri_parsing():
    assert _parse_uri("models:/churn-90d/3") == ("churn-90d", "version", "3")
    assert _parse_uri("models:/churn-90d@production") == ("churn-90d", "alias", "production")
    with pytest.raises(ValueError):
        _parse_uri("runs:/abc/model")


def test_registry_success_needs_no_fallback():
    def must_not_be_called(directory: Path) -> LoadedModel:
        raise AssertionError("bundled model used although the registry worked")

    model = load_model(Settings(), registry_loader=lambda s: loaded(), bundled_loader=must_not_be_called)
    assert model.info.source == "registry"


def test_registry_failure_retries_then_uses_the_bundled_model():
    attempts: list[int] = []
    sleeps: list[float] = []

    def down(settings):
        attempts.append(1)
        raise ConnectionError("connection refused")

    model = load_model(
        Settings(load_attempts=3, retry_seconds=2.0),
        registry_loader=down,
        bundled_loader=lambda d: loaded(source="bundled"),
        sleep=sleeps.append,
    )
    assert model.info.source == "bundled"
    assert len(attempts) == 3 and sleeps == [2.0, 2.0]


def test_both_failing_raises_with_every_reason():
    def down(settings):
        raise ConnectionError("connection refused")

    def missing(directory):
        raise FileNotFoundError(directory)

    with pytest.raises(ModelLoadError) as error:
        load_model(
            Settings(load_attempts=2), registry_loader=down, bundled_loader=missing, sleep=lambda s: None
        )
    assert "registry attempt 2: ConnectionError" in str(error.value)
    assert "bundled model: FileNotFoundError" in str(error.value)


def test_metadata_comes_from_registry_tags():
    tags = {"git_sha": "08ad93d", "decision_threshold": "0.920831", "data_version": "3ab4"}
    info = info_from_tags("churn-90d", "3", "models:/churn-90d/3", "registry", tags)
    assert (info.git_sha, info.decision_threshold, info.data_version) == ("08ad93d", 0.920831, "3ab4")


def test_bundled_copy_round_trip(tmp_path: Path):
    """A model saved the way fetch_model lays it out loads without any registry."""
    model = DummyClassifier(strategy="prior").fit([[0], [1], [1]], [0, 1, 1])
    mlflow.sklearn.save_model(model, str(tmp_path / "model"))
    metadata = {"name": "churn-90d", "version": "3", "uri": "models:/churn-90d/3",
                "tags": {"decision_threshold": "0.9", "git_sha": "08ad93d"}}  # fmt: skip
    (tmp_path / METADATA_FILE).write_text(json.dumps(metadata))
    bundled = load_bundled(tmp_path)
    assert bundled.info.source == "bundled" and bundled.info.version == "3"
    assert bundled.estimator.predict_proba([[0]])[0, 1] == pytest.approx(2 / 3)


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("MODEL_URI", "models:/churn-90d/3")
    monkeypatch.setenv("MODEL_LOAD_ATTEMPTS", "5")
    monkeypatch.setenv("BUNDLED_MODEL_DIR", "/srv/model")
    settings = Settings.from_env()
    assert (settings.model_uri, settings.load_attempts, str(settings.bundled_dir)) == (
        "models:/churn-90d/3",
        5,
        "/srv/model",
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A throwaway MLflow registry (SQLite) holding one tagged model version under alias 'production'."""
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setattr(mlflow, "get_tracking_uri", mlflow.get_tracking_uri)
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("serving-tests")
    with mlflow.start_run():
        info = mlflow.sklearn.log_model(
            DummyClassifier(strategy="prior").fit([[0], [1]], [0, 1]), name="model"
        )
    version = mlflow.register_model(info.model_uri, "churn-test")
    client = mlflow.MlflowClient()
    for key, value in {"git_sha": "abc1234", "decision_threshold": "0.75", "run_id": "r1"}.items():
        client.set_model_version_tag("churn-test", version.version, key, value)
    client.set_registered_model_alias("churn-test", "production", version.version)
    yield uri, version.version
    mlflow.set_tracking_uri("")


def test_load_from_registry_by_alias_pins_the_version(registry):
    from serving.model import load_from_registry

    uri, version = registry
    model = load_from_registry(Settings(model_uri="models:/churn-test@production", tracking_uri=uri))
    assert model.info.source == "registry"
    assert model.info.uri == f"models:/churn-test/{version}"  # alias resolved to a pinned version
    assert (model.info.git_sha, model.info.decision_threshold) == ("abc1234", 0.75)


def test_fetch_then_load_bundled_matches_the_registry(registry, tmp_path):
    from serving.fetch_model import fetch, main

    uri, version = registry
    metadata = fetch("models:/churn-test@production", tmp_path / "bundle")
    assert metadata["version"] == str(version) and metadata["tags"]["git_sha"] == "abc1234"
    bundled = load_bundled(tmp_path / "bundle")
    assert bundled.info.source == "bundled" and bundled.info.decision_threshold == 0.75
    assert main(["--uri", f"models:/churn-test/{version}", "--out", str(tmp_path / "again")]) == 0
