# Test pyramid

291 tests in total (`pytest --collect-only`), plus the in-cluster smoke test. Each level guards one step of
the path from code to a running deployment, and each level fails the step it guards.

| Level | What it checks | Where | Count | Runs | Evidence |
|---|---|---|---:|---|---|
| **1. Unit** | Features and point-in-time logic (`tests/pipeline/test_transform.py`), DQ rules and outcomes, metrics, splits, gate rules; **API**: input validation (required fields, ranges, nested windows, unknown fields → 422), request → model frame (defaults, derived fields, column order and dtypes = training), prediction wrapper, `/health` `/ready` `/predict` `/metrics`, registry → bundled → not-ready fallback, background reload | `tests/{data_quality,pipeline,training,serving}` | 242 | `make test-unit`, pre-push hook, CI | `evidence/tests/pytest_output.txt` |
| **1b. Integration** | The same code against a real Postgres: DQ gate end to end, pipeline components chained on a seeded schema, dataset loading | `-m integration` | 21 | `make test` (needs `DQ_TEST_DATABASE_URL`), CI with a Postgres service | `evidence/tests/pytest_output.txt` |
| **2. Model behavior** | On the registered model and the real holdout: **minimum performance** (PR-AUC ≥ 0.85, ROC-AUC ≥ 0.80, lift@10% ≥ 1.2, calibration within 0.05); **directional** (last order 30 days more recent / older, one extra order: probability may not move the wrong way by > 0.005); **invariance** (column order, batch vs single, omitted vs explicit defaults); **no crash** on edge cases (only required fields, extremes up to 1e9 spend, no activity, unseen country) | `tests/model` (`-m model`, `MODEL_URI`) | 13 | inside the **promotion gate** (`make promote`), `make test-model` | `evidence/k8s/promotion_gate_demo.txt` (v4 rejected by two directional tests) |
| **3. Container** | The built image starts and answers `/health`; registry reachable → serves the registry model; registry unreachable → serves the bundled copy; neither → alive but `/ready` and `/predict` 503; runs as uid 1000 | `tests/container` (`-m container`, `CHURN_API_IMAGE`) | 4 | `make test-container` after `make serving-image`, CI after the image build | `evidence/tests/container_tests.txt` |
| **4. Manifests** | `helm lint --strict`; `kubeconform -strict` against Kubernetes schemas; rendered rules: rolling update with `maxUnavailable: 0`, startup/liveness/readiness probes, requests and limits, non-root, read-only root fs, pinned model version; the chart **refuses** an empty or `latest` image tag and a model alias instead of a version | `tests/deploy` | 11 | `make test-unit`, pre-push, CI | `evidence/k8s/helm_lint.txt` |
| **5. Cluster smoke** | After every deploy, a `helm test` pod runs `serving.smoke` against the Service: `/health`, `/ready` with the **expected model version**, `/predict` schema and value ranges, 422 on invalid input, **p95 latency ≤ 300 ms** over 30 calls. Failure → `deploy/deploy.sh` rolls the release back and exits 1. A rollout whose pods never become ready is rolled back by `helm upgrade --rollback-on-failure`. | `deploy/churn-api/templates/tests/smoke-test.yaml`, `serving/smoke.py` | 1 suite | `make deploy`, CI (ephemeral kind, Part G) | `evidence/k8s/smoke_test_output.txt`, `evidence/k8s/rollback_demo.txt` |
| 6. ☆ Load test / canary | | | | not done | |

## How the levels gate each step

```
commit ──► pre-commit: ruff, ruff format, mypy
push   ──► pre-push: unit + manifest tests (no DB, no Docker)
train  ──► promotion gate: model behavior tests + holdout margin over production ──► alias "production"
build  ──► container tests on churn-api:<git sha>
deploy ──► helm upgrade --rollback-on-failure (probes) ──► helm test smoke ──► rollback on failure
```
