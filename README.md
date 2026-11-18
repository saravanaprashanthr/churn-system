# Time-series churn system

Predict whether a customer places **zero completed orders in the 90 days after a cutoff**, using only data from
before the cutoff, as a small production-style system:

**Postgres → SQL features (A) → data quality gate (B) → Kubeflow pipeline (C) → model + MLflow (D) → tested
Kubernetes deployment (F) → CI/CD (G) → drift, delayed labels and retraining (H)**, with code quality (E) throughout.

Evidence for every claim: [`EVIDENCE.md`](EVIDENCE.md) and [`evidence/`](evidence/). Trade-offs, cuts and scaling:
[`WRITEUP.md`](WRITEUP.md).

## Architecture

```
                     Postgres 16 (docker-compose)                              MLflow 3.18 server :5001
   customers products orders order_items events(10M)                          (Postgres backend, proxied
   sql/: indexes, BRIN, summary tables (A)  ·  dq_* (B)  ·  ml_* datasets (C)   artifacts) registry + aliases
        │                                                                          ▲           │ production
        ▼                                                                          │           ▼
  ┌──────────── KFP v2 pipeline, one run per cutoff (C) ─────────────┐   training/ (D)   promotion gate (F)
  │ extract ─► DQ gate ─► transform ─► validate ─► publish          │   time split +    behavior tests +
  │ (raw)     (40 rules,  (point-in-  (pandera)   (content-hash     │──►embargo, 6      PR-AUC ≥ prod+0.005
  │           quarantine) time feats)             dataset version)  │   iterations     │
  └──────────────────────────────────────────────────────────────────┘                  ▼
                                                                         churn-api:<git sha> (FastAPI)
  CronJob churn-retrain (H): train ─► gate                               Helm on kind: probes, HPA, PDB,
  monitoring/ (H): drift (PSI/KS) · delayed-label PR-AUC · alerts        helm-test smoke, auto-rollback (F)
           ▲                                                                       │ logs every prediction
           └────────────────────── ml_prediction_log ◄─────────────────────────────┘
  GitHub Actions (G): lint/mypy ─► tests (Postgres, 80%) ─► image + Trivy ─► kind deploy + smoke ─► GHCR ─► staging
                      prod: manual promote-prod workflow
```

## Quick start (macOS/Linux, clean machine)

Prerequisites: Docker (≥ 6 GB for the VM), `uv`, `git`, and for Parts F–H `kubectl`, `helm` (v4), `kind`,
`kubeconform`. The data zip from the Data Download Guide.

```bash
git clone <repo> && cd churn-system
mkdir -p data && unzip ecommerce_data.zip -d data     # five CSVs directly in data/
cp .env.example .env                                  # local practice credentials from SETUP.md
make install                                          # .venv (Python 3.11, pinned) + git hooks
make up                                               # Postgres :5432, Adminer :8080, MLflow :5001
make db-load                                          # schema + CSVs + DQ/dataset/MLflow tables (~5 min)
make lint test                                        # ruff, mypy, 290+ tests with an 80% coverage gate
```

Expected: `make db-load` prints `COPY 50000 / 2000 / 500000 / 1530225 / 10000000`; `make test` ends with
`Required test coverage of 80.0% reached`.

## Run each part

| Part | Command | Output |
|---|---|---|
| A. SQL performance | `make sql-optimize`; benchmarks: `sql/PERFORMANCE_REPORT.md` §6 | `sql/PERFORMANCE_REPORT.md`, `evidence/sql/` |
| B. Data quality | `make dq` (exit 0 pass, 1 blocked); demo: `scripts/dq_sandbox_demo.py` | `artifacts/dq/<run>/dq_report.md`, `data_quality/decision_log.md` |
| C. Pipeline | `make pipeline-image pipeline-compile`; `make pipeline-run CUTOFF=2026-03-30`; `make backfill` (65 cutoffs, ~40 min) | rows in `ml_dataset_versions`, `pipeline/pipeline.yaml` |
| D. Model | `make train` (needs the backfill) | MLflow experiment `churn-90d`, registry alias `candidate`; `training/model_card.md` |
| F. Promote + deploy | `make promote`; `make cluster`; `make deploy`; demos: `scripts/promotion_gate_demo.sh`, `deploy/rollback_demo.sh` | `deploy/RUNBOOK.md`, `evidence/k8s/` |
| G. CI/CD | push / PR (`.github/workflows/ci.yml`); prod: run `promote-prod` with an image tag | `evidence/ci/` |
| H. Monitoring | `deploy/retrain/install.sh` (CronJob); `python -m monitoring.drift`, `python -m monitoring.performance`; demo: `monitoring/drift_demo.sh` | `monitoring/RUNBOOK.md`, `evidence/monitoring/` |

`make help` lists every target.

## Repository layout

```
sql/            numbered SQL scripts (schema, optimizations, DQ/dataset/prediction-log tables) + PERFORMANCE_REPORT.md
data_quality/   40-rule SQL gate, quarantine, report, decision log
pipeline/       KFP components and pipeline, Dockerfile, compiled pipeline.yaml
training/       time-split training, promotion gate, model card, experiment log, trainer Dockerfile
serving/        FastAPI app, schemas, model loading with fallback, smoke test, Dockerfile
deploy/         Helm chart, kind config, deploy/rollback scripts, retraining CronJob, runbook
monitoring/     drift, delayed-label performance, replay, alert rules, runbook
tests/          unit · integration · model · container · deploy · monitoring
.github/        CI/CD workflows
evidence/       unedited command outputs for every part
```

## What I skipped (and what I would do next)

- **Real Kubeflow deployment (☆ C)**: ran with the KFP local DockerRunner; kind + KFP needs more than an 8 GB laptop
  has next to Postgres and MLflow. Steps are in `pipeline/README.md`.
- **MinIO (☆ D)**: MLflow uses Postgres for the backend and serves artifacts itself; MinIO would add an S3-style store.
- **Load test and canary (☆ F)**: not done. Next: k6 against the Service with a 300 ms p95 SLO, then a second
  Deployment for a 10% canary.
- **Long-lived staging/prod clusters (G)**: CI uses ephemeral kind clusters per job; prod approval is a manually
  started workflow, because GitHub Environment reviewers need a paid plan for private repos.
- **Prometheus server and dashboard (☆ H)**: metrics and alert rules are defined and tested, but no Prometheus or
  Grafana is running.
- **Status history**: `orders.status` is the current status, which limits point-in-time correctness (model card).
