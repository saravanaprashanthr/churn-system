#!/usr/bin/env bash
# Deploy the churn API to the current kube context, test it, and roll back if anything fails.
#
#   deploy/deploy.sh                    # image churn-api:<git sha>, model = registry alias 'production'
#   IMAGE_TAG=abc1234 MODEL_VERSION=3 deploy/deploy.sh
#
# 1. resolve the model version behind alias 'production' (unless MODEL_VERSION is given)
# 2. load the image into kind (when KIND_CLUSTER is set; CI and laptops use kind)
# 3. helm upgrade --rollback-on-failure --wait: a rollout whose pods never become ready is rolled back
# 4. helm test (serving.smoke in the cluster): on failure, roll back to the previous revision, exit 1
set -euo pipefail
cd "$(dirname "$0")/.."

NAMESPACE="${NAMESPACE:-churn}"
RELEASE="${RELEASE:-churn-api}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
KIND_CLUSTER="${KIND_CLUSTER-churn-system}"
TIMEOUT="${TIMEOUT:-4m}"

if [[ -z "${MODEL_VERSION:-}" ]]; then
  MODEL_VERSION="$(MLFLOW_DISABLE_AGENT_HINT=1 .venv/bin/python -c "
from mlflow import MlflowClient
print(MlflowClient().get_model_version_by_alias('churn-90d', 'production').version)" 2>/dev/null)"
fi
echo "deploying $RELEASE: image churn-api:$IMAGE_TAG, model churn-90d v$MODEL_VERSION -> namespace $NAMESPACE"

if [[ -n "$KIND_CLUSTER" ]]; then
  kind load docker-image "churn-api:$IMAGE_TAG" --name "$KIND_CLUSTER"
fi

helm upgrade --install "$RELEASE" deploy/churn-api \
  --namespace "$NAMESPACE" --create-namespace \
  --set image.tag="$IMAGE_TAG" --set model.version="$MODEL_VERSION" \
  --rollback-on-failure --wait=watcher --timeout "$TIMEOUT" --history-max 10

kubectl -n "$NAMESPACE" rollout status "deployment/$RELEASE" --timeout "$TIMEOUT"

if ! helm test "$RELEASE" --namespace "$NAMESPACE" --logs --timeout 3m; then
  echo "SMOKE TEST FAILED: rolling $RELEASE back to the previous revision" >&2
  helm rollback "$RELEASE" --namespace "$NAMESPACE" --wait=watcher --timeout "$TIMEOUT"
  helm history "$RELEASE" --namespace "$NAMESPACE" --max 5
  exit 1
fi
helm history "$RELEASE" --namespace "$NAMESPACE" --max 5
echo "deployed and smoke-tested: churn-api:$IMAGE_TAG with churn-90d v$MODEL_VERSION"
