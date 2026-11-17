#!/usr/bin/env bash
# Build the trainer image, load it into kind, create the DB secret from .env and apply the CronJob.
#   deploy/retrain/install.sh            (namespace churn)
# Trigger a run now:  kubectl -n churn create job retrain-manual-$(date +%s) --from=cronjob/churn-retrain
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a

NAMESPACE="${NAMESPACE:-churn}"
SHA="$(git rev-parse --short HEAD)"
export TRAINER_IMAGE="churn-trainer:$SHA"

docker build -q -f training/Dockerfile --build-arg GIT_SHA="$SHA" -t "$TRAINER_IMAGE" .
kind load docker-image "$TRAINER_IMAGE" --name "${KIND_CLUSTER:-churn-system}"

# The pod reaches Postgres on the host; the password stays out of git (it comes from .env).
POD_DATABASE_URL="${DATABASE_URL/@localhost:/@host.docker.internal:}"
kubectl -n "$NAMESPACE" create secret generic churn-retrain \
  --from-literal=DATABASE_URL="$POD_DATABASE_URL" --dry-run=client -o yaml | kubectl apply -f -

sed "s|\${TRAINER_IMAGE}|$TRAINER_IMAGE|" deploy/retrain/cronjob.yaml | kubectl -n "$NAMESPACE" apply -f -
kubectl -n "$NAMESPACE" get cronjob churn-retrain
