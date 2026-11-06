# Leakage checks: how we know features only use pre-cutoff data

The rule: at a cutoff, features may use only rows strictly before the cutoff, and the label is "no completed
order in [cutoff, cutoff + 90 days)". Five independent checks, from code design to the published data.

## 1. Design: the label window is split off before any feature code runs
`pipeline/transform.py` splits orders into `history` (`order_date < cutoff`) and `future`
(`cutoff ≤ order_date < cutoff + 90`) first. Feature functions only receive `history`; events are filtered to
`day < cutoff`. The extract itself never reads past `min(cutoff + 90, snapshot_date)`
(`evidence/pipeline/run_summary.md`: orders 2025-03-30 → 2026-06-28 for cutoff 2026-03-30).

## 2. Unit test: changing the future must not change any feature
`tests/pipeline/test_transform.py::test_features_do_not_change_when_anything_on_or_after_the_cutoff_changes`
sets every order on or after the cutoff to $10,000 and status `returned`, adds 50 extra orders at and after the
cutoff, and adds 500 events on the cutoff day. It asserts that every feature column is identical; only the label
may differ. Boundary tests check that an order at exactly `00:00` on the cutoff day is label, not feature, and
that an order at exactly `cutoff + 90 days` is outside the label window (`evidence/pipeline/component_tests.txt`).

## 3. Split: no training label is observed after the next split starts
`training/config.py::SplitConfig` refuses any split where `train_end + 90 days > valid_start` or
`valid_end + 90 days > test_start` (`tests/training/test_split_and_models.py`). The 12 embargo weeks between the
splits are used nowhere. Without this, a training row from May 2025 would carry an outcome from August 2025,
which wouldn't be known yet when the validation cutoffs are scored.

## 4. Data: every published feature value recomputed from raw orders before the cutoff
Run against Postgres after the backfill (all 65 labeled dataset versions):

```sql
WITH v AS (SELECT dataset_version, cutoff_date FROM ml_dataset_versions WHERE labeled),
recomputed AS (
  SELECT v.dataset_version, o.customer_id,
         COUNT(*) FILTER (WHERE o.order_date >= v.cutoff_date - 365)                   AS orders_365d,
         v.cutoff_date - MAX(o.order_date)::date                                        AS days_since_last_order,
         ROUND(SUM(o.order_total) FILTER (WHERE o.order_date >= v.cutoff_date - 90), 2) AS spend_90d
  FROM v JOIN orders o ON o.status = 'completed'
                      AND o.order_date >= v.cutoff_date - 365 AND o.order_date < v.cutoff_date
  WHERE NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = o.customer_id)
  GROUP BY 1, 2, v.cutoff_date
)
SELECT ... FROM ml_customer_features f JOIN v USING (dataset_version)
LEFT JOIN recomputed r USING (dataset_version, customer_id);
```

Output (unedited):

```
 published_rows | missing_in_recompute | orders_365d_mismatch | recency_mismatch | spend_90d_mismatch | min_days_since_last_order 
----------------+----------------------+----------------------+------------------+--------------------+---------------------------
          31296 |                    0 |                    0 |                0 |                  0 |                         1
(1 row)

 rows  | label_mismatch 
-------+----------------
 31296 |              0
(1 row)

```

All 31,296 published rows match the recomputation from strictly-before-cutoff orders, and the smallest
`days_since_last_order` is 1. No order dated on the cutoff day (or later) is in any feature. Every label matches
its 90-day window.

## 5. Population and exclusions are decided without future information
The population (≥1 completed order in the 365 days before the cutoff) and the DQ exclusions (implausible order
volume, quarantined orders) use only data up to the snapshot. Exclusions are applied to the whole
customer, not chosen per cutoff, so no rows are kept or dropped based on their label.

**Known residual risk:** `orders.status` is today's status, not the status as of the cutoff (no history table).
An order that was `pending` at the cutoff and is now `completed` counts as completed in the history features.
This can't be checked or fixed without status history, and it's listed in the model card.
