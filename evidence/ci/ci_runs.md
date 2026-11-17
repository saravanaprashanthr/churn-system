# CI runs (GitHub Actions, `.github/workflows/ci.yml`)

Repository: https://github.com/saravanaprashanthr/churn-system (private; add the reviewer to view the links).

| Run | Trigger | Result | What it shows | Log |
|---|---|---|---|---|
| [44117021336](https://github.com/saravanaprashanthr/churn-system/actions/runs/44117021336) | push to `main` | ✗ | lint ✓ and tests ✓ (Postgres service, 80% coverage gate); `build-and-deploy-test` failed at setup: I pinned `aquasecurity/trivy-action@0.33.1`, which does not exist. Fixed in `e961ed4` (`v0.36.0`). | (not saved) |
| [44118513207](https://github.com/saravanaprashanthr/churn-system/actions/runs/44118513207) | push to `main` (`e961ed4`) | ✓ **green** | lint/format/mypy → tests → image build, container tests, Trivy report → **ephemeral kind cluster: deploy.sh (rollback-on-failure) + helm test smoke** → push `churn-api:e961ed4` to GHCR → **deploy to `staging`** + smoke | `ci_green_run.txt` |
| [44122097767](https://github.com/saravanaprashanthr/churn-system/actions/runs/44122097767) | PR [#1](https://github.com/saravanaprashanthr/churn-system/pull/1) | ✗ **blocked** | A deliberately broken change: `order_date < cutoff` → `<= cutoff` in `pipeline/transform.py` ("include orders placed on the cutoff day"), which leaks the label window into the features. Pushed with `--no-verify` to skip the local pre-push hook, so CI is the safety net. **3 failed, 274 passed**: `test_order_at_the_cutoff_is_label_not_feature`, `test_features_do_not_change_when_anything_on_or_after_the_cutoff_changes`, and `test_valid_features_pass_with_report` (validation rejected the impossible values). Build and deploy never ran. PR closed unmerged. | `ci_red_run_blocked_change.txt` |

## Pipeline stages and where the assignment's requirements are met

1. **Lint, format check, type check ☆**: job `lint` (ruff, ruff format --check, mypy).
2. **Unit tests with coverage threshold**: job `test`, with a Postgres 16 service for the integration tests; `fail_under = 80`.
3. **Build image, vulnerability scan ☆**: `churn-api:<sha>` built from `serving/Dockerfile`; container tests; Trivy CRITICAL/HIGH, report only (`exit-code: 0`), so findings in the Debian base image are visible but don't block.
4. **Ephemeral kind cluster, deploy chart, cluster smoke tests**: `helm/kind-action`, then `deploy/deploy.sh` (`helm upgrade --rollback-on-failure --wait`, then `helm test`, which runs `serving.smoke` in the cluster).
5. **On merge to main: push the SHA-tagged image and deploy to staging; prod behind manual approval**: GHCR push and job `deploy-staging` (namespace `staging`). Prod only via `.github/workflows/promote-prod.yml`, which a person must start and which only accepts a tag with a green staging run. GitHub Environment reviewers are not available for private repos on the free plan (API: "billing plan does not support required reviewers"), so the manual start is the approval gate.
6. **Automatic rollback**: `--rollback-on-failure` undoes a rollout whose pods never become ready; a failed `helm test` triggers `helm rollback` (`deploy/deploy.sh`). Shown on the local cluster in `evidence/k8s/rollback_demo.txt`.

## Limitations

- "staging" and "prod" are namespaces in **ephemeral** kind clusters created per job; there is no long-lived cluster
  for CI to deploy into.
- CI has no access to the MLflow registry or the data, so it deploys a **stand-in model** trained on synthetic data
  (`scripts/build_ci_model.py`), with an unreachable registry. Every CI deploy therefore also exercises the
  registry-down fallback. Real model quality is gated by `training/promote.py`, which runs where the registry is.
