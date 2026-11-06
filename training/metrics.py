"""Metrics that fit a retention campaign: ranking quality, top-k capture, calibration and profit."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from training.config import CostConfig


def _top_k_mask(scores: np.ndarray, fraction: float) -> np.ndarray:
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    k = max(1, int(round(fraction * len(scores))))
    order = np.argsort(-scores, kind="stable")
    mask = np.zeros(len(scores), dtype=bool)
    mask[order[:k]] = True
    return mask


def recall_at_k(y: np.ndarray, scores: np.ndarray, fraction: float) -> float:
    """Share of all churners found among the top ``fraction`` highest-scored customers."""
    y = np.asarray(y)
    positives = y.sum()
    if positives == 0:
        return float("nan")
    return float(y[_top_k_mask(np.asarray(scores), fraction)].sum() / positives)


def precision_at_k(y: np.ndarray, scores: np.ndarray, fraction: float) -> float:
    y = np.asarray(y)
    return float(y[_top_k_mask(np.asarray(scores), fraction)].mean())


def lift_at_k(y: np.ndarray, scores: np.ndarray, fraction: float) -> float:
    """Churn rate in the top ``fraction`` divided by the overall churn rate (1.0 = no better than random)."""
    base = float(np.mean(y))
    return float("nan") if base == 0 else precision_at_k(y, scores, fraction) / base


def classification_metrics(y, scores, top_k_fractions: tuple[float, ...] = (0.1, 0.2)) -> dict[str, float]:
    y, scores = np.asarray(y), np.asarray(scores)
    out = {
        "pr_auc": float(average_precision_score(y, scores)),
        "roc_auc": float(roc_auc_score(y, scores)),
        "brier": float(brier_score_loss(y, scores)),
        "base_rate": float(y.mean()),
    }
    for fraction in top_k_fractions:
        pct = int(round(100 * fraction))
        out[f"recall_at_{pct}pct"] = recall_at_k(y, scores, fraction)
        out[f"precision_at_{pct}pct"] = precision_at_k(y, scores, fraction)
        out[f"lift_at_{pct}pct"] = lift_at_k(y, scores, fraction)
    return out


def campaign_profit(y, scores, threshold: float, value_if_saved, cost: CostConfig) -> dict[str, float]:
    """Expected profit of contacting everyone scored >= threshold.

    ``value_if_saved`` is per customer (gross margin of their expected spend over the horizon) or a scalar.
    """
    y, scores = np.asarray(y), np.asarray(scores)
    value = np.broadcast_to(np.asarray(value_if_saved, dtype=float), y.shape)
    contacted = scores >= threshold
    gain = float((y[contacted] * value[contacted] * cost.save_rate).sum())
    spend = float(contacted.sum() * cost.contact_cost)
    return {
        "threshold": float(threshold),
        "contacted": int(contacted.sum()),
        "contacted_share": float(contacted.mean()),
        "churners_reached": int(y[contacted].sum()),
        "expected_profit": gain - spend,
    }


def best_threshold(y, scores, value_if_saved, cost: CostConfig) -> dict[str, float]:
    """Threshold maximising expected profit, searched over every distinct score (and 'contact nobody')."""
    candidates = np.unique(np.concatenate([np.asarray(scores), [np.inf]]))
    results = [campaign_profit(y, scores, t, value_if_saved, cost) for t in candidates]
    return max(results, key=lambda r: (r["expected_profit"], r["threshold"]))


def breakeven_probability(value_if_saved: float, cost: CostConfig) -> float:
    """Calibrated churn probability above which contacting a customer pays off on average."""
    return cost.contact_cost / (cost.save_rate * value_if_saved)


def segment_metrics(frame: pd.DataFrame, y_col: str, score_col: str, segment_col: str) -> pd.DataFrame:
    rows = []
    for segment, part in frame.groupby(segment_col, observed=True):
        y, s = part[y_col].to_numpy(), part[score_col].to_numpy()
        both_classes = 0 < y.sum() < len(y)
        rows.append(
            {
                "segment": f"{segment_col}={segment}",
                "rows": len(part),
                "churn_rate": float(y.mean()),
                "mean_score": float(s.mean()),
                "pr_auc": float(average_precision_score(y, s)) if both_classes else float("nan"),
                "roc_auc": float(roc_auc_score(y, s)) if both_classes else float("nan"),
                "brier": float(brier_score_loss(y, s)),
            }
        )
    return pd.DataFrame(rows)
