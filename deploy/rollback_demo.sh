#!/usr/bin/env bash
# Rollback demo on the kind cluster, with traffic running inside the cluster throughout.
#   1. good rolling update to the current image
#   2. bad release: an image that crashes on start -> pods never ready -> automatic rollback
#   3. bad release: model version 999 -> pods fall back to the bundled model and look healthy,
#      but the smoke test (helm test) sees the wrong version -> deploy.sh rolls back
#   4. manual rollback: helm rollback to revision 1
# Usage: deploy/rollback_demo.sh [output file]
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export MLFLOW_DISABLE_AGENT_HINT=1
OUT="${1:-evidence/k8s/rollback_demo.txt}"
NS=churn
GOOD_TAG="$(git rev-parse --short HEAD)"
BAD_TAG=deadbee   # a hex "SHA" so the chart accepts it; the image is built to crash

state() {
  echo "--- helm history"; helm history churn-api -n "$NS" --max 10
  echo "--- serving image / model: $(kubectl -n "$NS" get deploy churn-api \
    -o jsonpath='{.spec.template.spec.containers[0].image} / {.metadata.labels.churn\.model/version}')"
  kubectl -n "$NS" get pods -l app.kubernetes.io/name=churn-api -o wide --no-headers | awk '{print "    pod", $1, $2, $3}'
}

start_traffic() {  # a pod POSTs to the Service every 100 ms and counts status codes
  kubectl -n "$NS" delete pod traffic --ignore-not-found --wait=true >/dev/null
  kubectl -n "$NS" run traffic --image="churn-api:$GOOD_TAG" --restart=Never --command -- python -c "
import collections, time, httpx
from serving.smoke import SAMPLE_CUSTOMER
codes, end = collections.Counter(), time.time() + $1
while time.time() < end:
    try:
        codes[httpx.post('http://churn-api:8080/predict', json={'instances': [SAMPLE_CUSTOMER]}, timeout=2).status_code] += 1
    except httpx.HTTPError as exc:
        codes[type(exc).__name__] += 1
    time.sleep(0.1)
print('TRAFFIC', dict(codes))" >/dev/null
  kubectl -n "$NS" wait --for=condition=Ready pod/traffic --timeout=60s >/dev/null
}

traffic_result() {
  kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/traffic --timeout=300s >/dev/null
  echo "--- traffic during this step (status code: requests): $(kubectl -n "$NS" logs traffic | grep TRAFFIC)"
}

{
  echo "# Rollback demo (deploy/rollback_demo.sh), $(date -u +%Y-%m-%dT%H:%MZ). Unedited output."
  echo "# Strategy: RollingUpdate maxUnavailable=0 maxSurge=1; deploy.sh = helm upgrade --rollback-on-failure"
  echo "# --wait, then helm test (serving.smoke); smoke failure -> helm rollback."
  echo; echo "## 0. Starting state"; state

  echo; echo "## 1. Good rolling update to churn-api:$GOOD_TAG"
  make serving-image >/dev/null 2>&1
  kind load docker-image "churn-api:$GOOD_TAG" --name churn-system >/dev/null 2>&1  # the traffic pod uses it
  start_traffic 75
  IMAGE_TAG="$GOOD_TAG" deploy/deploy.sh 2>&1 | grep -E "^deploying|rolled out|SMOKE TEST|deployed and|Error|FAILED"
  echo "exit code: ${PIPESTATUS[0]}"
  traffic_result; state

  echo; echo "## 2. Bad release: image churn-api:$BAD_TAG crashes on start"
  printf 'FROM churn-api:%s\nCMD ["python", "-c", "raise SystemExit(\\"simulated startup crash\\")"]\n' "$GOOD_TAG" \
    | docker build -q -t "churn-api:$BAD_TAG" - >/dev/null
  start_traffic 150
  IMAGE_TAG="$BAD_TAG" MODEL_VERSION=3 TIMEOUT=90s deploy/deploy.sh 2>&1 \
    | grep -E "^deploying|Error|failed|rollback|Rollback|SMOKE TEST|deployed and" | cut -c1-200
  echo "exit code: ${PIPESTATUS[0]}"
  traffic_result; state

  echo; echo "## 3. Bad release: model version 999 (does not exist; pods fall back to the bundled v3)"
  start_traffic 120
  IMAGE_TAG="$GOOD_TAG" MODEL_VERSION=999 deploy/deploy.sh 2>&1 \
    | grep -E "^deploying|serving model version|SMOKE TEST|Rollback|rollback|deployed and|Error" | cut -c1-200
  echo "exit code: ${PIPESTATUS[0]}"
  traffic_result; state

  echo; echo "## 4. Manual rollback: helm rollback churn-api 1"
  helm rollback churn-api 1 -n "$NS" --wait=watcher --timeout 4m
  echo "exit code: $?"
  kubectl -n "$NS" rollout status deploy/churn-api --timeout 4m
  state
  echo "--- kubectl rollout history"; kubectl -n "$NS" rollout history deploy/churn-api
  kubectl -n "$NS" delete pod traffic --ignore-not-found >/dev/null
} > "$OUT" 2>&1
echo "wrote $OUT"
