#!/usr/bin/env bash
# Promotion gate demo: reset the production aliases, then gate v1, v4, v3, v1 in order.
#   v1 = round-1 calibrated logistic regression   -> promoted (no production yet)
#   v4 = tree-only model: best PR-AUC, breaks the recency rule -> rejected by the behavior tests
#   v3 = ensemble                                  -> promoted (beats v1 by more than the margin)
#   v1 again                                       -> rejected (worse than production v3)
# Usage: scripts/promotion_gate_demo.sh [output file]   (needs .env, MLflow and Postgres running)
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export MLFLOW_DISABLE_AGENT_HINT=1 PYTHONWARNINGS=ignore
OUT="${1:-evidence/k8s/promotion_gate_demo.txt}"
LOG="$(mktemp)"

aliases() {
  .venv/bin/python -c "
from mlflow import MlflowClient
m = MlflowClient().get_registered_model('churn-90d')
print('aliases:', dict(sorted(m.aliases.items())))" 2>/dev/null
}

.venv/bin/python -c "
from mlflow import MlflowClient, exceptions
c = MlflowClient()
for alias in ('production', 'previous-production'):
    try:
        c.delete_registered_model_alias('churn-90d', alias)
    except exceptions.MlflowException:
        pass" 2>/dev/null

{
  echo "# Promotion gate demo (scripts/promotion_gate_demo.sh). Rules: behavior tests (tests/model) pass,"
  echo "# holdout unseen, holdout PR-AUC >= 0.85 and >= production + 0.005. Holdout = test split, cutoffs"
  echo "# 2026-01-05..2026-03-30. v1 = round-1 calibrated logistic regression; v3 = ensemble; v4 = tree-only"
  echo "# model (good metrics, breaks the recency rule). Unedited output, $(date -u +%Y-%m-%dT%H:%MZ)."
  echo; echo "## Before (production aliases reset)"; aliases
  for version in 1 4 3 1; do
    echo; echo "## \$ python -m training.promote --candidate models:/churn-90d/$version"
    .venv/bin/python -m training.promote --candidate "models:/churn-90d/$version" > "$LOG" 2>&1
    code=$?
    grep -E "INFO promote|ERROR" "$LOG" | cut -c25-
    echo "exit code: $code"
    aliases
  done
} > "$OUT" 2>&1
rm -f "$LOG"
echo "wrote $OUT"
