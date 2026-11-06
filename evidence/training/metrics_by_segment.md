# Metrics by segment (test split, final model `churn-90d` v3)

Generated from `artifacts/training/metrics_by_segment.csv` (also logged to the MLflow run `5_ensemble`, `evaluation/metrics_by_segment.csv`). Test = 13 weekly cutoffs 2026-01-05 … 2026-03-30, 6,435 rows. `mean_score` vs `churn_rate` shows calibration per segment; AUCs show ranking *within* the segment.

| Segment | Rows | Churn rate | Mean score | PR-AUC | ROC-AUC | Brier |
|---|---:|---:|---:|---:|---:|---:|
| `orders_365d_bucket=1` | 3,565 | 0.901 | 0.882 | 0.914 | 0.589 | 0.088 |
| `orders_365d_bucket=2-3` | 1,191 | 0.639 | 0.718 | 0.669 | 0.557 | 0.234 |
| `orders_365d_bucket=4+` | 1,679 | 0.195 | 0.205 | 0.585 | 0.861 | 0.114 |
| `recency_bucket=<=30d` | 1,499 | 0.320 | 0.326 | 0.865 | 0.938 | 0.094 |
| `recency_bucket=31-90d` | 1,410 | 0.643 | 0.652 | 0.899 | 0.852 | 0.146 |
| `recency_bucket=91-180d` | 1,391 | 0.784 | 0.785 | 0.868 | 0.755 | 0.139 |
| `recency_bucket=181-365d` | 2,135 | 0.854 | 0.862 | 0.917 | 0.720 | 0.115 |
| `tenure_bucket=<1y` | 1,254 | 0.683 | 0.673 | 0.935 | 0.889 | 0.108 |
| `tenure_bucket=1-2y` | 1,231 | 0.677 | 0.682 | 0.872 | 0.813 | 0.154 |
| `tenure_bucket=2y+` | 3,950 | 0.661 | 0.673 | 0.898 | 0.880 | 0.117 |
| `browsed_30d=no` | 623 | 0.632 | 0.663 | 0.925 | 0.912 | 0.104 |
| `browsed_30d=yes` | 5,812 | 0.672 | 0.676 | 0.896 | 0.865 | 0.124 |
| `country=AU` | 736 | 0.624 | 0.642 | 0.932 | 0.919 | 0.112 |
| `country=BR` | 636 | 0.742 | 0.748 | 0.879 | 0.781 | 0.111 |
| `country=CA` | 674 | 0.620 | 0.677 | 0.832 | 0.807 | 0.169 |
| `country=DE` | 492 | 0.742 | 0.637 | 0.944 | 0.906 | 0.111 |
| `country=FR` | 599 | 0.608 | 0.652 | 0.831 | 0.856 | 0.142 |
| `country=GB` | 549 | 0.636 | 0.620 | 0.955 | 0.928 | 0.099 |
| `country=IN` | 684 | 0.703 | 0.702 | 0.922 | 0.857 | 0.111 |
| `country=US` | 2,065 | 0.675 | 0.684 | 0.904 | 0.877 | 0.120 |
| `marketing_opt_in=no` | 2,373 | 0.655 | 0.682 | 0.870 | 0.844 | 0.136 |
| `marketing_opt_in=unknown` | 650 | 0.689 | 0.681 | 0.944 | 0.886 | 0.125 |
| `marketing_opt_in=yes` | 3,412 | 0.674 | 0.669 | 0.913 | 0.886 | 0.112 |

## Where the model fails

- **Low-frequency customers.** One-time buyers (55% of rows, churn 0.90) get ROC-AUC 0.59, and 2–3-order customers 0.56 with the worst Brier (0.234). The model knows these groups are high risk but can barely rank customers inside them. The overall ROC-AUC of 0.87 comes mostly from separating frequent from infrequent buyers.
- **Long recency.** Within 181–365 days since the last order, ROC-AUC falls to 0.72 (vs 0.94 for ≤ 30 days).
- **Country calibration.** DE is under-predicted (mean score 0.637 vs churn 0.742) and CA/FR over-predicted (0.677 vs 0.620; 0.652 vs 0.608), even though the ranking inside DE is good (ROC-AUC 0.91).
- **Fine where it matters most for the threshold.** Customers who bought recently or often are ranked well (ROC-AUC 0.86–0.94), and the high-score region used by the 0.921 threshold reaches 92% precision on test.

Feature importance (permutation, PR-AUC drop on test): `orders_365d` 0.107, `returned_90d` 0.008, `cancelled_90d` 0.006, `spend_365d` 0.006, `country` 0.003; browsing features ≤ 0.002. Full list: `artifacts/training/permutation_importance.csv` / MLflow run artifact.
