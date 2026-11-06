# Data preparation pipeline (Kubeflow Pipelines v2)

```
extract ──► data-quality-gate ──► transform ──► validate ──► publish
raw rows     Part B gate:          features +    pandera      content-hashed dataset in
(Parquet)    quarantine + report;  90-day        schema and   ml_dataset_versions /
             fails on `block`      label         sanity       ml_customer_features
```

One run builds the dataset for **one cutoff date**. A backfill is a series of runs; every run is idempotent.

| Step | Code | Reads | Writes (KFP artifact) |
|---|---|---|---|
| extract | `extract.py` | customers; orders in `[cutoff − lookback, min(cutoff + horizon, snapshot))`; events in `[cutoff − 90 d, cutoff)` aggregated per customer/day/type in SQL | `raw`: 3 Parquet files + manifest |
| data-quality-gate | `dq_step.py` → `data_quality/` | all source tables, as of `snapshot_date` | `report` (dq_report.json/md, quarantine sample), `exclusions` (excluded customers, quarantined order ids); output `dq_run_id` |
| transform | `transform.py` | `raw`, `exclusions` | `features`: one row per customer, 27 features + `churned` |
| validate | `validate.py` | `features` | `validation` report; fails on any problem |
| publish | `publish.py` | `features` | rows in Postgres; `dataset` artifact; output `dataset_version` |

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `cutoff_date` | (required) | features use data strictly before it; label = no completed order in `[cutoff, cutoff + horizon)` |
| `snapshot_date` | 2026-07-01 | the date the data is "as of" (DQ gate `as_of`); a cutoff is labeled only if `cutoff + horizon ≤ snapshot` |
| `lookback_days` | 365 | population (≥1 completed order in it) and longest feature window |
| `horizon_days` | 90 | label window |
| `dq_lookback_days` | 28 | days checked by the DQ volume rules |
| `min_rows` | 50 | validation fails below this |
| `source_db_url` | `postgresql://ds_candidate@host.docker.internal:5432/ecommerce` | **no password**: libpq reads `PGPASSWORD` from the environment |

## Design decisions

- **Population = customers with ≥1 completed order in the 365 days before the cutoff**, minus DQ exclusions:
  ~480–505 customers per cutoff, churn rate ~67%. All customers with events (~48k) would give a ~100% churn
  label that carries no information: 98% of customers never order.
- **Point-in-time.** Orders are split into history (`< cutoff`) and label window before any feature code runs.
  `tests/pipeline/test_transform.py::test_features_do_not_change_when_anything_on_or_after_the_cutoff_changes`
  rewrites every row on or after the cutoff and asserts the features don't change.
- **The DQ gate runs after extract** (as the assignment orders the steps) and on the source tables, so the
  extract is a faithful raw snapshot and transform applies the gate's exclusions. A blocked gate raises, so
  nothing downstream runs. The gate itself is idempotent per `snapshot_date` (same run id), so running it once
  per pipeline run costs time (~25 s) but never changes results.
- **Versioned dataset.** `dataset_version = <cutoff>-<sha256[:12]>` over the parameters plus a canonical CSV of the
  rows. Same inputs → same version → publish writes nothing (`created=False`). Different data, parameters or
  snapshot → new version. Training selects versions from `ml_dataset_versions`.
- **Containers.** All five components use one image, `churn-pipeline:<git-sha>` (`pipeline/Dockerfile`), with the
  pinned `requirements/pipeline.txt`. Components are created with `install_kfp_package=False`, so nothing is
  installed at run time. The image runs as a non-root user.
- **Caching off.** Idempotence comes from the steps. KFP cache hits would hide a re-run.
- **Secrets.** The password never appears as a parameter or in `pipeline.yaml`. Locally, the DockerRunner passes
  `PGPASSWORD` from the shell; on a cluster it comes from a Kubernetes secret (below).

## Run

```bash
SHA=$(git rev-parse --short HEAD)
docker build -f pipeline/Dockerfile --build-arg GIT_SHA=$SHA -t churn-pipeline:$SHA .
export PIPELINE_IMAGE=churn-pipeline:$SHA
.venv/bin/python -m pipeline.pipeline --output pipeline/pipeline.yaml            # compile to IR YAML
PGPASSWORD=ds_candidate_pw .venv/bin/python -m pipeline.run_local --cutoff 2026-03-30          # one cutoff
PGPASSWORD=ds_candidate_pw .venv/bin/python -m pipeline.run_local \
    --backfill-start 2025-01-06 --backfill-end 2026-03-30                        # 65 weekly cutoffs
```

KFP local artifacts land in `artifacts/kfp_local/`. Tests: `DQ_TEST_DATABASE_URL=... pytest tests/pipeline`.

## Running on a real KFP deployment (☆, not done)

Not run here: the laptop has 8 GB RAM in total, and a kind cluster plus Kubeflow Pipelines needs about 8 GB on
its own, on top of Postgres. The steps would be:

1. `kind create cluster`, then install KFP standalone:
   `kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=2.4.0"` and
   `.../env/platform-agnostic?ref=2.4.0`.
2. `kind load docker-image churn-pipeline:<sha>` (or push to a registry the cluster can pull from).
3. Create the DB secret: `kubectl -n kubeflow create secret generic churn-db --from-literal=PGPASSWORD=...`, and in
   `pipeline.py` add `kfp.kubernetes.use_secret_as_env(task, "churn-db", {"PGPASSWORD": "PGPASSWORD"})` to the
   tasks that connect.
4. Point `source_db_url` at a host the cluster can reach (a Postgres Service, or `host.docker.internal` from kind).
5. Upload and run: `kfp.Client(host=...).create_run_from_pipeline_package("pipeline/pipeline.yaml",
   arguments={"cutoff_date": "2026-03-30"})`. A backfill becomes one run per date, or a recurring run.
