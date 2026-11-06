# Experiment log: 90-day churn

All runs: MLflow experiment `churn-90d` (`evidence/training/mlflow_runs.csv`; each run tagged `training_round`).
Data: 65 weekly dataset versions from the Part C pipeline (cutoffs 2025-01-06 … 2026-03-30, 31,296 rows,
`data_version` `3ab46a6bee7b`). Split by cutoff with a 90-day embargo between splits:

| Split | Cutoffs | Rows | Churn rate |
|---|---|---:|---:|
| train | 2025-01-06 … 2025-05-12 (19) | 8,905 | 0.667 |
| valid | 2025-08-11 … 2025-10-06 (9) | 4,323 | 0.672 |
| test | 2026-01-05 … 2026-03-30 (13) | 6,435 | 0.668 |

Selection metric: **validation PR-AUC** (churn = positive class). Test is scored only for the baseline and the
final model. With a 67% churn rate, precision cannot exceed 1.0, so **lift is capped at 1/0.668 ≈ 1.50**.

## Round 1 (commit `308614a`)

| Run | What changed | Valid PR-AUC | Valid ROC-AUC | Valid Brier | Verdict |
|---|---|---:|---:|---:|---|
| heuristic_recency | rank by days since last order, no model | 0.858 | 0.785 | n/a | reference point |
| 0_baseline_logreg | standardised features, one-hot categoricals, logistic regression | **0.908** | 0.862 | 0.122 | strong |
| 1_gradient_boosting | HistGradientBoosting, defaults | 0.893 | 0.845 | 0.142 | **worse** than baseline |
| 2_gb_derived_features | + recency vs usual order gap, recent spend share, single-order flag, log spend | 0.897 | 0.849 | 0.137 | helped GB (+0.004), kept |
| 3_gb_tuned | Optuna TPE, 40 trials on valid PR-AUC | 0.906 | **0.872** | 0.120 | closed the gap; best ROC-AUC |
| 4_calibrated | isotonic calibration of the best ranker (= baseline) on valid | 0.905 | 0.865 | 0.120 | **hurt on test** |

**What round 1 showed**
- The **plain logistic regression beat gradient boosting**. Optuna's best trees were tiny and heavily
  regularised (5 leaves, ≥154 samples per leaf, learning rate 0.011): the signal is mostly monotonic ("fewer and
  older orders → more churn"), and ~9k training rows don't support much interaction structure.
- **Isotonic calibration made the baseline worse on test** (Brier 0.1248 vs 0.1244; PR-AUC 0.8891 vs 0.8908).
  Logistic regression was already well calibrated, and the isotonic map overfit the 4,323 validation rows. Its
  stepwise output also put the top group at exactly 1.0, which produced the odd threshold of 1.0.
- Design flaw found: calibration was applied unconditionally. Fixed in round 2.

## Round 2 (commit `08ad93d`, registered as `churn-90d` v3)

Rounds 1–3 are unchanged (same seeds, same numbers). New runs:

| Run | What changed | Valid PR-AUC | Valid ROC-AUC | Valid Brier | Verdict |
|---|---|---:|---:|---:|---|
| 4_logreg_log_derived | logistic regression + derived features + log1p on heavy-tailed counts and spend (spend_365d mean $1,852, median $374) | 0.9075 | 0.863 | 0.123 | **no gain** (−0.0004 vs baseline) |
| 5_ensemble | soft vote: baseline logistic regression + tuned GB | **0.9121** | 0.869 | **0.1195** | **best**, chosen |
| calibration_check | calibrator fit on first 5 valid weeks, Brier on last 4: none 0.1199, sigmoid 0.1214, isotonic 0.1190 | | | | gain 0.0009 < 0.001 bar → **not calibrated** |

**Final model on test (scored once):**

| Model | PR-AUC | ROC-AUC | Brier | Recall@10% | Lift@10% | Precision@10% | Recall@20% | Lift@20% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0_baseline_logreg | 0.8908 | 0.8634 | 0.1244 | 0.132 | 1.320 | 0.882 | 0.275 | 1.376 |
| **5_ensemble** | **0.8987** | **0.8701** | **0.1221** | **0.135** | **1.345** | **0.899** | **0.277** | **1.387** |

The ensemble beats the baseline on every test metric: +0.008 PR-AUC, +0.007 ROC-AUC, −0.002 Brier. The gain is
real but small. The two members are close in quality but make different mistakes: the tree model ranks better
overall (ROC-AUC), the linear one is better at the top (PR-AUC).

## What helped, what didn't

| Idea | Result |
|---|---|
| Gradient boosting instead of logistic regression | ✗ worse until tuned, then still slightly below on PR-AUC |
| Derived features (recency vs usual order gap, …) | ✓ for GB (+0.004 PR-AUC); ✗ for logistic regression |
| log1p on skewed counts/spend for logistic regression | ✗ no gain: standardised raw features were already enough for ranking |
| Optuna tuning (40 trials) | ✓ +0.009 PR-AUC for GB; found that very simple trees work best |
| Isotonic calibration, unconditional (round 1) | ✗ worse test Brier and PR-AUC; caused a degenerate threshold of 1.0 |
| Calibration only if it helps on later held-out weeks (round 2) | ✓ correctly declined both times it was not worth it |
| Soft-vote ensemble of the best linear and tree models | ✓ best on valid and test |
| Class-imbalance handling | **not tried, on purpose**: churners are the *majority* (67%), so reweighting would not address a real problem |

## Process issues, recorded honestly

- An attempt between the rounds **crashed** while saving a sigmoid-calibrated model: MLflow saves with skops, and
  `_SigmoidCalibration` was not in the trusted-type list. Its runs are tagged
  `training_round = round2-aborted`. A unit test now saves every model type the code can produce and checks it
  against the trust list.
- Round 2 was first run from **uncommitted code** (git SHA `308614a-dirty`, registry v2). It was rerun from
  commit `08ad93d` (v3, alias `candidate`). The numbers were identical, which also confirms the training is
  reproducible. v1 and v2 carry a `status` tag saying why they are superseded.

## Next steps I would try

- Rolling-origin validation over several folds instead of one valid window: 9 validation weeks make differences
  of ±0.005 PR-AUC uncertain.
- Features for one-time buyers (55% of test rows, where the model is weakest, see `metrics_by_segment.md`):
  first-order basket and category, days from signup to first order.
- A cost/uplift-aware objective once there is campaign response data; see the threshold sensitivity in the model card.
