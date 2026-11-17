"""Load the model and its metadata, with a defined behavior when the registry is unreachable.

1. Load ``MODEL_URI`` (a pinned registry version, e.g. ``models:/churn-90d/3``) from the MLflow registry,
   ``MODEL_LOAD_ATTEMPTS`` times with a short timeout per HTTP call (MLflow's own default retries
   for minutes).
2. If that fails, load the copy bundled into the image at build time (``serving/fetch_model.py``) and
   report ``source: "bundled"`` in every response.
3. If both fail, raise ``ModelLoadError``: the API stays alive (``/health`` 200) but not ready
   (``/ready`` and ``/predict`` 503) and keeps retrying in the background.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from serving.schemas import ModelInfo

logger = logging.getLogger(__name__)

METADATA_FILE = "metadata.json"


class ModelLoadError(RuntimeError):
    """Neither the registry nor the bundled copy could be loaded."""


@dataclass(frozen=True)
class Settings:
    model_uri: str = "models:/churn-90d@production"
    tracking_uri: str | None = None
    bundled_dir: Path = Path("/app/model")
    load_attempts: int = 3
    retry_seconds: float = 2.0
    http_timeout_seconds: int = 5
    reload_interval_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            model_uri=env.get("MODEL_URI", cls.model_uri),
            tracking_uri=env.get("MLFLOW_TRACKING_URI"),
            bundled_dir=Path(env.get("BUNDLED_MODEL_DIR", str(cls.bundled_dir))),
            load_attempts=int(env.get("MODEL_LOAD_ATTEMPTS", cls.load_attempts)),
            retry_seconds=float(env.get("MODEL_LOAD_RETRY_SECONDS", cls.retry_seconds)),
            http_timeout_seconds=int(env.get("MLFLOW_HTTP_TIMEOUT_SECONDS", cls.http_timeout_seconds)),
            reload_interval_seconds=float(
                env.get("MODEL_RELOAD_INTERVAL_SECONDS", cls.reload_interval_seconds)
            ),
        )


@dataclass
class LoadedModel:
    estimator: Any
    info: ModelInfo
    loaded_at: float = field(default_factory=time.time)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.estimator.predict_proba(frame)[:, 1], dtype=float)


def _parse_uri(uri: str) -> tuple[str, str, str]:
    """('churn-90d', 'version' | 'alias', '3' | 'production') from models:/name/3 or models:/name@alias."""
    match = re.fullmatch(r"models:/([^/@]+)(?:/(\d+)|@([\w-]+))", uri)
    if not match:
        raise ValueError(
            f"MODEL_URI must look like models:/<name>/<version> or models:/<name>@<alias>: {uri}"
        )
    name, version, alias = match.groups()
    return (name, "version", version) if version else (name, "alias", alias)


def info_from_tags(name: str, version: str, uri: str, source: str, tags: dict[str, str]) -> ModelInfo:
    return ModelInfo(
        name=name,
        version=str(version),
        uri=uri,
        source=source,  # type: ignore[arg-type]
        run_id=tags.get("run_id"),
        git_sha=tags.get("git_sha"),
        data_version=tags.get("data_version"),
        decision_threshold=float(tags.get("decision_threshold", 0.5)),
    )


def load_from_registry(settings: Settings) -> LoadedModel:
    import mlflow
    from mlflow import MlflowClient

    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", str(settings.http_timeout_seconds))
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
    if settings.tracking_uri:
        mlflow.set_tracking_uri(settings.tracking_uri)
    name, kind, ref = _parse_uri(settings.model_uri)
    client = MlflowClient()
    version = (
        client.get_model_version_by_alias(name, ref)
        if kind == "alias"
        else client.get_model_version(name, ref)
    )
    pinned_uri = f"models:/{name}/{version.version}"
    estimator = mlflow.sklearn.load_model(pinned_uri)
    return LoadedModel(estimator, info_from_tags(name, version.version, pinned_uri, "registry", version.tags))


def load_bundled(directory: Path) -> LoadedModel:
    import mlflow

    metadata = json.loads((directory / METADATA_FILE).read_text())
    estimator = mlflow.sklearn.load_model(str(directory / "model"))
    info = info_from_tags(metadata["name"], metadata["version"], metadata["uri"], "bundled", metadata["tags"])
    return LoadedModel(estimator, info)


def load_model(
    settings: Settings,
    registry_loader: Callable[[Settings], LoadedModel] = load_from_registry,
    bundled_loader: Callable[[Path], LoadedModel] = load_bundled,
    sleep: Callable[[float], None] = time.sleep,
) -> LoadedModel:
    errors: list[str] = []
    for attempt in range(1, settings.load_attempts + 1):
        try:
            model = registry_loader(settings)
            logger.info("loaded %s v%s from the registry", model.info.name, model.info.version)
            return model
        except Exception as exc:  # noqa: BLE001 - any registry failure triggers the defined fallback
            errors.append(f"registry attempt {attempt}: {type(exc).__name__}: {exc}")
            logger.warning("registry load attempt %d/%d failed: %s", attempt, settings.load_attempts, exc)
            if attempt < settings.load_attempts:
                sleep(settings.retry_seconds)
    try:
        model = bundled_loader(settings.bundled_dir)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bundled model: {type(exc).__name__}: {exc}")
        raise ModelLoadError("; ".join(errors)) from exc
    logger.warning("registry unreachable; serving the bundled %s v%s", model.info.name, model.info.version)
    return model
