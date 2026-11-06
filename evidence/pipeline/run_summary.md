# Pipeline run summary

Runner: KFP 2.18.0 local **DockerRunner**. Every step ran in its own container from `churn-pipeline:1fb0592`
(built from commit `1fb0592`, `pipeline/Dockerfile`). Compiled IR: `evidence/pipeline/pipeline.yaml` (identical to
`pipeline/pipeline.yaml`). Raw logs: `run_log.txt` (single run), `backfill_log.txt` (re-run / multi-date demo).

## Single run: cutoff 2026-03-30 (`run_log.txt`)

**Parameters**

| Parameter | Value |
|---|---|
| cutoff_date | 2026-03-30 |
| snapshot_date | 2026-07-01 |
| lookback_days | 365 |
| horizon_days | 90 |
| dq_lookback_days | 28 |
| min_rows | 50 |
| source_db_url | `postgresql://ds_candidate@host.docker.internal:5432/ecommerce` (password via `PGPASSWORD`) |

**Artifacts produced** (under `artifacts/kfp_local/churn-dataset-prep-2026-11-05-23-41-03-277572/`)

| Step | Artifact | Content |
|---|---|---|
| extract | `raw/` | `customers.parquet` 50,000 rows · `orders.parquet` 244,132 rows (2025-03-30 → 2026-06-28) · `events_daily.parquet` 1,243,748 rows (2025-12-30 → 2026-03-30) · `manifest.json` |
| data-quality-gate | `report/dq_20260701/` | `dq_report.json`, `dq_report.md`, `quarantine_sample.csv`: **PASS**, 6 warnings, 19,970 rows quarantined |
| data-quality-gate | `exclusions/` | 23 excluded customers, 10,016 quarantined order ids, `dq_run_id.txt` = `dq_20260701` |
| transform | `features/features.parquet` | 481 customers × (27 features + label) |
| validate | `validation` | passed, 481 rows, churn rate 0.680, no problems |
| publish | `dataset/features.parquet` + 481 rows in `ml_customer_features` | **dataset_version `20260330-3c4085f162f3`** (created) |

## Re-run and multi-date demo (`backfill_log.txt`)

Cutoffs 2026-03-16, 2026-03-23 and 2026-03-30, run after the single run above:

| Cutoff | Rows | Churn rate | dataset_version | created |
|---|---:|---:|---|---|
| 2026-03-16 | 483 | 0.667 | `20260316-662e953d2fcb` | True |
| 2026-03-23 | 480 | 0.671 | `20260323-8d9d58efa59a` | True |
| 2026-03-30 | 481 | 0.680 | `20260330-3c4085f162f3` | **False**: same version as the single run, nothing written |

The repeated cutoff produced the identical content hash. `ml_dataset_versions.created_at` for it still shows the
first run (18:11:43 UTC), and `ml_customer_features` still holds exactly 481 rows for it.
