"""Model definitions for each iteration. Every model is one sklearn Pipeline, so preprocessing is saved
with the model and serving cannot apply it differently from training."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from training.config import SEED
from training.data import CATEGORICAL

DERIVED_FEATURES = ("recency_vs_gap", "spend_share_90d", "single_order", "log_spend_365d")

# Our own functions referenced by the saved pipelines. MLflow saves sklearn models with skops, which only
# loads types it is told to trust; listing them is safer than falling back to pickle.
SKOPS_TRUSTED_TYPES = (
    "training.models._numeric",
    "training.models._categorical",
    "training.models.add_derived_features",
    # skops flags this because a crafted file could hold out-of-range node indices. Our model files are
    # only ever written by this training job into our own registry, so the type is trusted for them.
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
    "sklearn.calibration._CalibratedClassifier",  # internal holder of the fitted model + isotonic map
)

DEFAULT_HGB_PARAMS: dict[str, Any] = {
    "learning_rate": 0.1,
    "max_iter": 100,
    "max_leaf_nodes": 31,
    "min_samples_leaf": 20,
    "l2_regularization": 0.0,
}


def add_derived_features(X: pd.DataFrame) -> pd.DataFrame:
    """Ratios a tree finds hard to build from raw counts.

    recency_vs_gap: days since the last order relative to this customer's usual gap between orders
    (365 / orders in the last year); > 1 means "later than usual".
    """
    X = X.copy()
    usual_gap = 365.0 / X["orders_365d"].clip(lower=1)
    X["recency_vs_gap"] = X["days_since_last_order"] / usual_gap
    X["spend_share_90d"] = (X["spend_90d"] / X["spend_365d"].where(X["spend_365d"] > 0)).fillna(0.0)
    X["single_order"] = (X["orders_365d"] == 1).astype("int64")
    X["log_spend_365d"] = np.log1p(X["spend_365d"])
    return X


def _numeric(X: pd.DataFrame) -> list[str]:
    return [c for c in X.columns if c not in CATEGORICAL]


def _categorical(X: pd.DataFrame) -> list[str]:
    return [c for c in X.columns if c in CATEGORICAL]


def _preprocessor(scale: bool) -> ColumnTransformer:
    numeric = StandardScaler() if scale else "passthrough"
    return ColumnTransformer(
        [
            ("numeric", numeric, _numeric),
            ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), _categorical),
        ],
        verbose_feature_names_out=False,
    )


def _steps(derived: bool) -> list[tuple[str, Any]]:
    return [("derive", FunctionTransformer(add_derived_features, validate=False))] if derived else []


def logistic_regression(derived: bool = False) -> Pipeline:
    model = LogisticRegression(max_iter=2000, random_state=SEED)
    return Pipeline([*_steps(derived), ("prep", _preprocessor(scale=True)), ("model", model)])


def gradient_boosting(params: dict[str, Any] | None = None, derived: bool = False) -> Pipeline:
    model = HistGradientBoostingClassifier(
        **{**DEFAULT_HGB_PARAMS, **(params or {})}, early_stopping=False, random_state=SEED
    )
    return Pipeline([*_steps(derived), ("prep", _preprocessor(scale=False)), ("model", model)])


def calibrate(
    fitted: Pipeline, X_calibration: pd.DataFrame, y_calibration: pd.Series
) -> CalibratedClassifierCV:
    """Isotonic calibration of an already-fitted model on held-out (later) data; the model is not refit."""
    calibrated = CalibratedClassifierCV(FrozenEstimator(fitted), method="isotonic")
    return calibrated.fit(X_calibration, y_calibration)


def hgb_search_space(trial) -> dict[str, Any]:
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "max_iter": trial.suggest_int("max_iter", 50, 500, step=50),
        "max_leaf_nodes": trial.suggest_int("max_leaf_nodes", 4, 64, log=True),
        "min_samples_leaf": trial.suggest_int("min_samples_leaf", 10, 200, log=True),
        "l2_regularization": trial.suggest_float("l2_regularization", 1e-6, 10.0, log=True),
    }


def churn_probability(model, X: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(X)[:, 1]
