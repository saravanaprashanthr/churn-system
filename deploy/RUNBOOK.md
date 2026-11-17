# Churn API: deployment runbook

The API serves one **pinned** MLflow model version (`churn-90d/<n>`) behind a Helm release `churn-api` in
namespace `churn`. Images are `churn-api:<git sha>`; the chart refuses `latest` and refuses a model alias.

## Prerequisites

```bash
make up          # Postgres :5432, MLflow :5001 (macOS AirPlay owns :5000)
make cluster     # kind cluster "churn-system" + metrics-server (for the HPA)
cp .env.example .env
```

## Normal path: promote, then deploy

```bash
make train                                        # registers a new version with alias 'candidate'
make promote                                      # gate: behavior tests + holdout PR-AUC >= production + 0.005
make deploy                                       # build churn-api:<sha>, deploy alias 'production', smoke-test
```

`deploy/deploy.sh` resolves the version behind `production`, runs `helm upgrade --rollback-on-failure --wait`
(the rollout is rolled back if the new pods never become ready), then `helm test` (the in-cluster smoke test:
health, readiness with the **expected model version**, response schema, 422 on bad input, p95 ≤ 300 ms).
If the smoke test fails, it runs `helm rollback` and exits 1.

## Verify

```bash
kubectl -n churn get deploy,pods,svc,hpa
helm history churn-api -n churn                   # each revision is described "churn-api:<sha> model v<n>"
kubectl -n churn port-forward svc/churn-api 8080:8080
curl localhost:8080/ready                         # model name, version, source (registry | bundled), git SHA
.venv/bin/python -m serving.smoke --url http://localhost:8080 --expect-version <n>
```

## Roll back

| Problem | Do this |
|---|---|
| Bad **release** (image or config) | `helm rollback churn-api <revision> -n churn --wait=watcher` (find the revision with `helm history`) |
| Bad **model** already promoted | Point `production` back: `MlflowClient().set_registered_model_alias("churn-90d", "production", <previous>)`; the gate keeps the old one as alias `previous-production`. Then `make deploy`. |
| Both | Model alias first, then `make deploy` (the image does not need to change: the model version is a Helm value). |

Rollbacks are fast because the previous ReplicaSet is kept (`revisionHistoryLimit: 10`) and the model is pinned
in the ConfigMap. Evidence: `evidence/k8s/rollback_demo.txt`.

## When the model registry is unreachable

The pods do not need MLflow to keep serving. At startup a pod tries the registry 3 times with a 5 s timeout, then
serves the model **bundled into the image** at build time, and says so in every response
(`"source": "bundled"`). If neither works, the pod is alive (`/health` 200) but not ready (`/ready` 503), so it
gets no traffic and retries every 30 s. Already-running pods are unaffected by a registry outage.

Check: `curl :8080/ready | jq .model.source`. If `bundled` and that version is not what you expect, fix MLflow
and restart the pods: `kubectl -n churn rollout restart deploy/churn-api`.

## Troubleshooting

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Pods `0/1 Running`, never ready | model not loadable | `kubectl -n churn logs deploy/churn-api`; `curl :8080/ready` shows the reason (registry attempts, bundled error) |
| `ErrImageNeverPull` / `ImagePullBackOff` | image not loaded into kind | `kind load docker-image churn-api:<sha> --name churn-system` (deploy.sh does this) |
| `helm template` fails "image.tag must be a git SHA" | tag empty or `latest` | pass `IMAGE_TAG=<sha>`; never deploy `latest` |
| Smoke test fails "serving model version X (got Y)" | wrong `MODEL_VERSION`, or the registry was unreachable and the bundled model answered | check `/ready` `source`; deploy.sh has already rolled back |
| Smoke test fails on p95 latency | CPU throttling or a cold start | `kubectl top pods -n churn`; HPA scales 2 → 4 at 70% CPU |
| Pods can't reach MLflow | `host.docker.internal:5001` unreachable from kind, or the host is not in MLflow's `--allowed-hosts` | `kubectl -n churn exec deploy/churn-api -- python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:5001/health').read())"` |
| `kubectl` talks to the wrong cluster | another kind cluster is current | `kubectl config use-context kind-churn-system` |

## Settings

Helm values (`deploy/churn-api/values.yaml`): replicas 2 (HPA 2–4 at 70% CPU), requests 100m CPU / 256 Mi,
limits 1 CPU / 512 Mi (measured ~193 Mi in use), probes startup `/health` (90 s budget), liveness `/health`,
readiness `/ready`, PodDisruptionBudget `minAvailable: 1`, non-root, read-only root filesystem with `/tmp` as
emptyDir. Secrets (e.g. an MLflow token) go in `secret.data` and reach the pod as environment variables.
