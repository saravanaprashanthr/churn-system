# Model card: `churn-90d` v3 (alias `candidate`)

| | |
|---|---|
| Model | soft-vote ensemble: logistic regression + HistGradientBoosting (sklearn 1.9.1), preprocessing inside the pipeline |
| Registry | MLflow `churn-90d` version 3, alias `candidate` (promotion to production only through the Part F gate) |
| Code / data | git `08ad93d` · `data_version` `3ab46a6bee7b` (65 dataset versions, snapshot 2026-07-01) |
| Output | probability that the customer places **zero completed orders in the 90 days after the cutoff** |
| Decision threshold | **0.921** under the default cost assumptions (see below; a business setting, not a model property) |

## Intended use

Rank **existing customers** (≥1 completed order in the last 365 days) by churn risk at a weekly cutoff, so a
retention team can decide whom to send an offer. Scores are meant for ranking and for an expected-value
decision. They are **not** a statement about an individual's intentions.

**Not intended for:** customers who never bought (98% of the customer base; that's an activation problem, and the
model never saw them); the 23 accounts excluded by the data quality gate (implausible order volume); pricing,
credit or any decision with legal or financial effect on the person; horizons other than 90 days.

## Data

- Source: the Part C pipeline. One row per eligible customer per weekly cutoff, 27 point-in-time features
  (orders and spend over 7/30/90/365 days, recency, trend, cancellations/returns, 30-day browsing activity,
  tenure, country, marketing opt-in) and the label.
- Leakage controls: features use rows strictly before the cutoff; label windows of a split close before the next
  split starts (90-day embargo). Verified on the real data: `evidence/training/leakage_checks.md`.
- Splits: train 8,905 rows (2025-01-06 … 2025-05-12), valid 4,323 (2025-08-11 … 2025-10-06), test 6,435
  (2026-01-05 … 2026-03-30). Churn rate ≈ 0.67 in all three.

## Performance (test split, scored once)

| Metric | Model | Baseline (logistic regression) |
|---|---:|---:|
| PR-AUC | **0.899** | 0.891 |
| ROC-AUC | **0.870** | 0.863 |
| Brier score | **0.122** | 0.124 |
| Recall@top 10% / Lift@top 10% | 0.135 / 1.345 | 0.132 / 1.320 |
| Recall@top 20% / Lift@top 20% | 0.277 / 1.387 | 0.275 / 1.376 |

Lift cannot exceed ≈1.50 here, because two thirds of these customers churn. For reference, ranking by days since
the last order alone gives validation PR-AUC 0.858 (model: 0.912).

### By segment (test; full table in `metrics_by_segment.md`)

| Segment | Rows | Churn rate | ROC-AUC | Note |
|---|---:|---:|---:|---|
| 1 order in the last year | 3,565 | 0.90 | 0.59 | **weak**: most one-time buyers churn, and the model can barely tell which come back |
| 2–3 orders | 1,191 | 0.64 | 0.56 | **weakest**: worst Brier (0.234) |
| 4+ orders | 1,679 | 0.19 | 0.86 | good |
| last order ≤ 30 days ago | 1,499 | 0.32 | 0.94 | good |
| country = DE | 492 | 0.74 | 0.91 | **under-predicted** (mean score 0.64 vs 0.74 actual) |
| country = CA | 674 | 0.62 | 0.81 | over-predicted (0.68 vs 0.62) |

Most of the overall ROC-AUC comes from separating segments (frequent vs one-time buyers). **Within** the low-
frequency segments, ranking is close to random.

### What drives it (permutation importance on test, PR-AUC drop)

`orders_365d` 0.107 ≫ `returned_90d` 0.008 · `cancelled_90d` 0.006 · `spend_365d` 0.006 · `country` 0.003 · …
The 30-day browsing features contribute almost nothing (≤ 0.002). Caveat: correlated features (e.g. recency
vs `orders_365d`) share credit under permutation, so a low score doesn't always mean a feature is useless.

## Decision threshold and cost assumptions

Assumptions (`training/config.py`, to be confirmed with the retention team): contacting a customer costs **$5**;
a contacted churner is kept with probability **20%**; a kept customer is worth **30% gross margin** on their last
year's spend rate over 90 days. The threshold maximising expected profit on **validation** is **0.921**. Applied to test:

| Policy | Contacted | Churners reached | Expected profit |
|---|---:|---:|---:|
| score ≥ 0.921 | 118 (1.8%) | 108 (92% precision) | **+$511** |
| contact everyone | 6,435 | 4,301 | −$8,109 |
| contact nobody | 0 | 0 | $0 |

**Sensitivity:** the best policy changes completely with the assumptions.

| Contact cost | Save rate | Threshold | Contacted (test) | Profit | Contact everyone |
|---:|---:|---:|---:|---:|---:|
| $5 | 20% | 0.921 | 1.8% | $511 | −$8,109 |
| $2 | 20% | 0.075 | 87.6% | $11,431 | $11,196 |
| $1 | 20% | 0.031 | 91.7% | $18,164 | $17,631 |
| $5 | 40% | 0.075 | 87.6% | $17,228 | $15,957 |

When contacts are cheap, contacting almost everyone is nearly as good as using the model, because most customers
churn. The model adds value mainly when offers are expensive. The threshold must come from real campaign costs.

## Limitations and risks

- **Small, specific population**: about 480–505 eligible customers per week. After excluding 23 bot-like accounts
  that place 97% of all orders, the real customer base is about 920 buyers.
- `orders.status` is the **current** status, not the status at the cutoff (no history table). A late
  cancellation can change a past label.
- `signup_date` is unreliable (3% of orders predate it); tenure uses the earlier of signup and first order.
- Browsing events don't link to orders (97.6% of `purchase` events come from customers with no orders), so event
  features are weak behaviour signals at best.
- Validated on one 9-week window and tested on one 13-week window. Differences of about ±0.005 PR-AUC between
  models are within noise.
- Country is a feature, and two countries are mis-calibrated (DE, CA). Check this before any country-level policy.
- Scores are not calibrated beyond what the models produce (a calibration check found no reliable gain).

## Maintenance

Retrain weekly with `python -m training.train` once new labels mature (90 days). Promotion only through the
Part F gate (tests + margin over production on the holdout). Drift and delayed-label performance monitoring:
Part H.
