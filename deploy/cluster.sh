#!/usr/bin/env bash
# Create the local kind cluster and install metrics-server (needed by the HorizontalPodAutoscaler).
# Idempotent: an existing cluster is reused.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${KIND_CLUSTER:-churn-system}"
METRICS_SERVER_VERSION="v0.7.2"

if kind get clusters | grep -qx "$CLUSTER"; then
  echo "cluster $CLUSTER exists"
else
  kind create cluster --config deploy/kind-config.yaml --name "$CLUSTER" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER"

kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"
# kind's kubelets use self-signed certificates.
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' \
  2>/dev/null || true
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
kubectl get nodes -o wide
