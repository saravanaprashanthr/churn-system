# EVIDENCE

Candidate ID: <to fill>
Approx. hours spent: <to fill>
Environment: macOS 14.5 (Apple Silicon), 8 GB RAM, Docker Desktop 29.9.2 (VM: 8 vCPU / 5.8 GB), PostgreSQL 16, kind v0.33.0
Tools used, including AI assistants, and for what: Claude Code (AI coding assistant) helped with data exploration, writing the Part A SQL and benchmark scripts, and running the benchmarks. <add your own description>

## Summary
| Part | Status (Done / Partial / Skipped) | Key paths | One-line result |
|------|-----------------------------------|-----------|-----------------|
| A. Time-series SQL        | Done | `sql/`, `sql/PERFORMANCE_REPORT.md`, `evidence/sql/` | Warm medians: A1 1,580 → 785 ms, A2 88 → 16 ms, A3a 91 → 33 ms, A3b 701 → 21 ms; all results identical (two-way `EXCEPT ALL`) |
| B. Data quality           | Done | `data_quality/`, `tests/data_quality/`, `sql/02_dq_tables.sql`, `evidence/dq/` | 40 SQL rules over 7 dimensions, block/warn + quarantine; full data: PASS (0 blocking, 6 warnings, 19,970 rows quarantined, 25 s); blocks correctly on stale data; 137 tests, 98% coverage |
| C. Data prep pipeline     | Done (☆ real KFP cluster: not done) | `pipeline/`, `pipeline/pipeline.yaml`, `evidence/pipeline/` | KFP v2, 5 containerized steps on `churn-pipeline:1fb0592`; DockerRunner runs; idempotent backfill of 65 cutoffs → 31,296 rows; repeat run returns same content-hash version |
| D. Model development      | Done | `training/`, MLflow `churn-90d` v3, `evidence/training/` | Embargoed time split; baseline + 5 tracked iterations; final ensemble test PR-AUC 0.899 vs baseline 0.891; cost-based threshold 0.921; features recomputed from raw data: 0 mismatches |
| E. Code quality & tests   | Done (coverage gate in CI: Part G) | `Makefile`, `.pre-commit-config.yaml`, `pyproject.toml`, `evidence/tests/`, `evidence/lint/` | 203 tests, 93% coverage (gate 80%); ruff incl. no-print and bandit rules, ruff format, mypy (☆) all clean; 10 pre-commit hooks + unit tests on push |
| F. Kubernetes deployment  | Done (☆ load test/canary: not done) | `serving/`, `deploy/`, `training/promote.py`, `tests/{serving,model,container,deploy}`, `evidence/k8s/` | FastAPI on kind via Helm; promotion gate rejects a higher-PR-AUC model that breaks the recency rule; auto-rollback of a crashing image and of a wrong model version; 0 failed requests during all rollouts |
| G. CI/CD                  | Not started | | |
| H. MLOps & monitoring     | Not started | | |

## Part A: Time-series SQL
- **Claims → evidence**
  - Tested at full scale (orders 500,000, order_items 1,530,225, events 10,000,000) → `evidence/sql/row_counts.txt`
  - Median of 5 runs, cold and warm, before vs after: A1 1,781/1,580 → 797/785 ms; A2 117/88 → 16/16 ms;
    A3a 104/91 → 39/33 ms; A3b 1,089/701 → 280/21 ms → `evidence/sql/timings.md`, `evidence/sql/benchmark_*.csv`
  - Plans for every query before and after → `evidence/sql/<Q>_before_explain.txt`, `evidence/sql/<Q>_after_explain.txt`
  - Optimized queries return identical results (0 rows in both `EXCEPT ALL` directions, A1: 2,533,912 rows) → `evidence/sql/equality_check.txt`
  - BRIN with `minmax_multi` reads 87% fewer blocks than plain `minmax`, which the scattered future-dated events had
    made useless → `evidence/sql/events_window_before_explain.txt`, `evidence/sql/events_window_after_explain.txt`
- **Techniques used and why:** composite/covering B-tree indexes (per-customer lookups); `LATERAL ... LIMIT 1` in place of
  a self-join and correlated subquery; sargable predicates; pre-aggregated summary tables with an idempotent
  incremental refresh; BRIN `minmax_multi` on `events.event_ts` (correlation 0.997; not on `orders.order_date`,
  correlation −0.008); fresh statistics, exact row estimates and `jit = off`. Details: `sql/PERFORMANCE_REPORT.md` §3.
- **Costs / trade-offs:** +407 MB of indexes (+42%); inserting 200k events is ~7× slower (279 → 1,956 ms); the B-tree
  bloated 301 → 394 MB after rolled-back inserts until `REINDEX`; summary tables lag `orders` by up to one refresh
  (daily); A3b gains 33× warm but only 3.9× cold (random I/O) → `evidence/sql/index_cost.txt`, `evidence/sql/refresh_timings.txt`
- **Not done / limitations:** range partitioning (considered; see report §3); A1 is bounded by producing 2.53M output
  rows (~450 ms of 785 ms); `orders.status` is the current status, not the status on the "as of" date (no history table);
  23 high-volume accounts are excluded by rule (`sql/01_dq_excluded_customers.sql`), to be formalised in Part B.
- **How to reproduce:** `sql/PERFORMANCE_REPORT.md` §6.

## Part B: Data quality
- **Claims → evidence**
  - Gate run on the full official data (`as_of` 2026-07-01): PASS, 40 rules, 0 blocking, 6 warnings,
    19,970 rows quarantined, exit code 0 → `evidence/dq/dq_report.json`, `evidence/dq/dq_report.md`, `evidence/dq/gate_run.log`
  - Quarantine output (10 rows per quarantining rule) → `evidence/dq/quarantine_sample.csv`
  - The gate blocks: run 10 days past the end of the data, freshness and volume-drop rules block, exit code 1 →
    `evidence/dq/blocked_run.log`
  - Every issue found, what was done and why (15 entries) → `evidence/dq/decision_log.md` (copy of `data_quality/decision_log.md`)
  - 137 tests pass (unit + 15 integration tests against Postgres), 98% coverage of `data_quality/` → `evidence/dq/test_output.txt`
- **Techniques used and why:** custom SQL rules run inside Postgres (no extraction of 10M events), each with a
  dimension, severity, tolerance and optional quarantine; deterministic run id per `as_of` for idempotent
  re-runs; one transaction per run. Why not Great Expectations / pandera: `data_quality/decision_log.md`.
- **Costs / trade-offs:** ~25 s per run, mostly the full scans of events for the duplicate/id checks; tolerances
  for known issues are judgment calls set just above observed rates; rules check whole tables, not just
  the run window (fine at this size, not at 100×).
- **Not done / limitations:** late-arriving rows cannot be detected (no ingestion timestamp); order status
  history is not available; no alerting integration yet (Part H).
- **How to reproduce:** `data_quality/README.md`.

## Part C: Data preparation pipeline
- **Claims → evidence**
  - Compiled IR YAML, every step on the SHA-tagged image → `evidence/pipeline/pipeline.yaml` (= `pipeline/pipeline.yaml`)
  - One run via the KFP local DockerRunner (5 containers: extract → DQ gate → transform → validate → publish) →
    `evidence/pipeline/run_log.txt`; parameters, artifacts and dataset version → `evidence/pipeline/run_summary.md`
  - Re-run / multi-date demo: repeating cutoff 2026-03-30 returns the same version `20260330-3c4085f162f3`
    with `created=False` → `evidence/pipeline/backfill_log.txt`
  - Full backfill, 65 cutoffs, 62 new + 3 existing versions, 31,296 rows → `evidence/pipeline/backfill_full_summary.txt`
  - Component tests: 42 passed, 94% coverage, including a leakage test and an integration test chaining the real
    component functions on a seeded schema → `evidence/pipeline/component_tests.txt`
- **Techniques used and why:** lightweight KFP components on one pinned image (`install_kfp_package=False`, so nothing
  is installed at run time); content-hash dataset versions for idempotent publishing; the DB password reaches
  containers via `PGPASSWORD`, never as a parameter; caching off so re-runs really run; pandera for the feature
  table. Details: `pipeline/README.md`.
- **Costs / trade-offs:** the DQ gate runs in every pipeline run (~25 s of ~37 s) although it is idempotent per
  snapshot; one run per cutoff instead of a `ParallelFor`; population limited to recent buyers (~480–505 per cutoff).
- **Not done / limitations:** ☆ real KFP deployment on kind (8 GB RAM); steps to do it are in `pipeline/README.md`.
- **How to reproduce:** `pipeline/README.md` → Run.

## Part D: Model development
- **Claims → evidence**
  - Every run with params, metrics, git SHA, data version, model and signature → `evidence/training/mlflow_runs.csv`
    (30 runs, tagged by `training_round`); registry `churn-90d` v3 alias `candidate` (git `08ad93d`)
  - Baseline + 5 iterations, what helped and what didn't, including a failed round → `evidence/training/experiment_log.md`
  - Test (scored once): ensemble PR-AUC 0.899 / ROC-AUC 0.870 / Brier 0.122 vs baseline 0.891 / 0.863 / 0.124 →
    `evidence/training/train_log.txt`
  - Error analysis by segment and feature importance → `evidence/training/metrics_by_segment.md`
  - Leakage: all 31,296 published feature rows and labels recomputed from raw data, 0 mismatches →
    `evidence/training/leakage_checks.md`
  - Model card with intended use, cost assumptions, threshold sensitivity, limitations → `evidence/training/model_card.md`
  - 24 tests, 91% coverage of `training/` → `evidence/training/test_output.txt`
- **Techniques used and why:** split by cutoff with a 90-day embargo (labels of one split close before the next
  starts); selection on valid PR-AUC, test once; Optuna (seeded TPE); calibration only if it improves Brier on
  later held-out validation weeks; profit-maximising threshold from stated costs; permutation importance;
  MLflow server with a Postgres backend (☆ partly: artifacts proxied by the server instead of MinIO).
- **Costs / trade-offs:** one validation window (9 weeks), so small differences are noisy; the threshold depends
  heavily on the cost assumptions (sensitivity table in the model card); boosting did not beat the linear model alone.
- **Not done / limitations:** MinIO artifact store; rolling-origin CV; uplift modelling.
- **How to reproduce:** `docker compose up -d mlflow`, then
  `DATABASE_URL=... MLFLOW_TRACKING_URI=http://localhost:5001 python -m training.train`.

## Part E: Code quality
- **Claims → evidence**
  - `pytest -v --cov`: 203 passed, 93.4% coverage of `data_quality/`, `pipeline/`, `training/`; `fail_under = 80`
    in `pyproject.toml` → `evidence/tests/pytest_output.txt`
  - ruff lint (incl. `T20` no-print and `S` bandit security rules) and format check clean → `evidence/lint/ruff.txt`,
    `evidence/lint/ruff_format.txt`
  - mypy (☆) over all packages, tests and scripts: no issues in 41 files → `evidence/lint/mypy.txt`
  - pre-commit, all hooks pass → `evidence/lint/pre_commit.txt`
- **What is in place:** packages `data_quality/`, `pipeline/`, `training/`, no logic only in notebooks; type hints
  throughout; logging instead of print (print allowed only in the demo script and two CLI result lines, each
  listed with a reason in `pyproject.toml`); configuration from environment / `.env` (`.env.example` documents
  every variable, `.env` is git-ignored, the compose password is overridable); every dependency pinned in
  `requirements/*.txt`, resolved together; `Makefile` with `make test`, `make lint`, `make run` and every other
  task; pre-commit hooks (whitespace, YAML/TOML, merge markers, large files, private keys, ruff, ruff format, mypy)
  plus the unit tests on `pre-push`, all from the project venv so versions match the lock. `evidence/` is
  excluded from hooks so outputs stay unedited.
- **Git history:** small commits with conventional messages; tests committed with or before the code they test
  (e.g. `feat(dq): declarative rule catalog with static tests`). Security-rule exemptions are per line, each with a reason.
- **Not done / limitations:** the coverage gate is enforced locally and by `make test`; enforcing it in CI is Part G.
- **How to reproduce:** `make install && make lint && make test` (with `DQ_TEST_DATABASE_URL` set, see `.env.example`).

## Part F: Test-driven deployment to Kubernetes
- **Claims → evidence**
  - Cluster, objects, deployment, rollout status/history → `evidence/k8s/cluster_info.txt`, `get_all.txt`,
    `describe_deployment.txt`, `rollout_status.txt`, `rollout_history.txt`; `helm lint --strict` → `helm_lint.txt`
  - Deploy + in-cluster smoke test (`helm test`): model v3 from the registry, p95 15 ms (budget 300 ms) →
    `evidence/k8s/smoke_test_output.txt`; pod logs → `logs_sample.txt`
  - Rollback demo with in-cluster traffic: good rolling update (554 × 200), crashing image auto-rolled back
    (1,145 × 200), model v999 caught by the smoke test and rolled back (889 × 200), manual `helm rollback` →
    `evidence/k8s/rollback_demo.txt`
  - Promotion gate: v1 promoted (no production), **v4 rejected** with the best holdout PR-AUC (0.907) because two
    directional tests fail, v3 promoted (0.899 ≥ 0.889 + 0.005), v1 rejected → `evidence/k8s/promotion_gate_demo.txt`
  - Test pyramid by level and where each runs → `evidence/k8s/test_pyramid.md`; container tests →
    `evidence/tests/container_tests.txt`
- **Techniques used and why:** pinned model version in the ConfigMap (rollback restores image and model); registry →
  bundled → not-ready loading with 5 s timeouts; image tagged with the git SHA, chart refuses `latest` and aliases;
  `maxUnavailable: 0`, startup/liveness/readiness probes, measured requests/limits, HPA, PDB, non-root, read-only root
  fs; `helm upgrade --rollback-on-failure` + `helm test` with rollback; directional tests with a stated 0.005 tolerance.
- **Costs / trade-offs:** a 918 MB image (scikit-learn, pandas, MLflow client); directional checks use a tolerance
  because the ensemble's tree member is not strictly monotonic; one-node kind cluster (no node-failure testing).
- **Not done / limitations:** ☆ load test and canary/blue-green; the `latest` guard covers the chart, not a registry policy.
- **How to reproduce:** `deploy/RUNBOOK.md`; `make cluster && make promote && make deploy`;
  `deploy/rollback_demo.sh`; `scripts/promotion_gate_demo.sh`.

## Biggest trade-offs I made and what I would do with more time
<to fill at the end>
