#!/usr/bin/env bash
# Capture the Part F evidence files listed in the Submission Guide (evidence/k8s/), from the live cluster.
set -uo pipefail
cd "$(dirname "$0")/.."
NS=churn
DEPLOY=churn-api
DIR=evidence/k8s
mkdir -p "$DIR"

{ kubectl version; echo; kubectl get nodes -o wide; } > "$DIR/cluster_info.txt" 2>&1
kubectl -n "$NS" get all -o wide > "$DIR/get_all.txt" 2>&1
kubectl -n "$NS" get hpa,pdb,configmap >> "$DIR/get_all.txt" 2>&1
kubectl -n "$NS" describe deploy "$DEPLOY" > "$DIR/describe_deployment.txt" 2>&1
kubectl -n "$NS" rollout status deploy/"$DEPLOY" > "$DIR/rollout_status.txt" 2>&1
{ kubectl -n "$NS" rollout history deploy/"$DEPLOY"; echo; helm history "$DEPLOY" -n "$NS"; } \
  > "$DIR/rollout_history.txt" 2>&1
helm lint deploy/churn-api --strict --set image.tag="$(git rev-parse --short HEAD)" --set model.version=3 \
  > "$DIR/helm_lint.txt" 2>&1
{
  for pod in $(kubectl -n "$NS" get pods -l app.kubernetes.io/name=churn-api -o name); do
    echo "=== $pod"; kubectl -n "$NS" logs "$pod" --tail=15
  done
} > "$DIR/logs_sample.txt" 2>&1
echo "captured into $DIR: $(ls "$DIR" | tr '\n' ' ')"
