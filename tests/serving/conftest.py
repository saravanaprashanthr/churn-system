"""A fake model and app factory, so API tests need no MLflow and no database."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from serving.app import create_app
from serving.model import LoadedModel, ModelLoadError, Settings
from serving.schemas import ModelInfo
from serving.smoke import SAMPLE_CUSTOMER

INFO = ModelInfo(
    name="churn-90d", version="7", uri="models:/churn-90d/7", source="registry", run_id="abc",
    git_sha="1234567", data_version="feedbeef0000", decision_threshold=0.5,
)  # fmt: skip


class RecencyModel:
    """Churn probability rises with days since the last order: enough to test the API around it."""

    def __init__(self) -> None:
        self.frames: list[pd.DataFrame] = []

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        self.frames.append(frame)
        p = (frame["days_since_last_order"].to_numpy(dtype=float) / 365.0).clip(0, 1)
        return np.column_stack([1 - p, p])


def loaded(source: str = "registry") -> LoadedModel:
    return LoadedModel(RecencyModel(), INFO.model_copy(update={"source": source}))


def customer(**overrides) -> dict:
    return {**SAMPLE_CUSTOMER, **overrides}


def lapsed(days_since_last_order: int = 300, **overrides) -> dict:
    """A consistent customer whose last order is older than 90 days (no recent orders or spend)."""
    quiet = {"orders_7d": 0, "orders_30d": 0, "orders_90d": 0, "orders_prev_30d": 0, "active_days_90d": 0,
             "spend_7d": 0.0, "spend_30d": 0.0, "spend_90d": 0.0}  # fmt: skip
    return customer(**quiet, days_since_last_order=days_since_last_order, **overrides)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(), loader=lambda settings: loaded())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unready_client() -> Iterator[TestClient]:
    def fail(settings: Settings) -> LoadedModel:
        raise ModelLoadError("registry attempt 1: ConnectionError; bundled model: FileNotFoundError")

    app = create_app(Settings(reload_interval_seconds=3600), loader=fail)
    with TestClient(app) as test_client:
        yield test_client
