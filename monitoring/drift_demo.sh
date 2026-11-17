#!/usr/bin/env bash
# Drift demo: shifted traffic -> drift alert -> retraining job -> promotion gate -> rollout.
#   monitoring/drift_demo.sh [output file]
# Starts from an older model in production (v1), so a fresh retrain can legitimately beat it.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export MLFLOW_DISABLE_AGENT_HINT=1 PYTHONWARNINGS=ignore
OUT="${1:-evidence/monitoring/drift_demo_log.txt}"
NS=churn
PORT=18080
mkdir -p "$(dirname "$OUT")" artifacts/monitoring
POD_LOG_URL="${DATABASE_URL/@localhost:/@host.docker.internal:}"
LOG_ARGS="--set secret.data.PREDICTION_LOG_DATABASE_URL=$POD_LOG_URL"

say() { echo; echo "## $(date -u +%H:%M:%S) $*"; }
serving() { curl -s "localhost:$PORT/ready" | .venv/bin/python -c "import json,sys; m=json.load(sys.stdin)['model']; print('serving: churn-90d v'+m['version'], '('+m['source']+')')"; }
forward() {
  pkill -f "port-forward svc/churn-api $PORT" 2>/dev/null; sleep 1
  kubectl -n "$NS" port-forward svc/churn-api "$PORT:8080" >/dev/null 2>&1 &
  for _ in $(seq 1 30); do curl -sf "localhost:$PORT/ready" >/dev/null && return; sleep 1; done
}
# Phase markers from the database clock: logged_at is stamped by Postgres, whose VM clock can drift from the host.
now() { docker exec ds_interview_postgres psql -U ds_candidate -d ecommerce -At -c "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00')"; }

{
  echo "# Drift demo (monitoring/drift_demo.sh), $(date -u +%Y-%m-%dT%H:%MZ). Unedited output."
  say "0. Setup: production = v1 (an older model), deployed with prediction logging"
  .venv/bin/python -c "
from mlflow import MlflowClient
c = MlflowClient()
c.set_registered_model_alias('churn-90d', 'production', '1')
print('aliases:', dict(sorted(c.get_registered_model('churn-90d').aliases.items())))" 2>/dev/null
  MODEL_VERSION=1 HELM_EXTRA_ARGS="$LOG_ARGS" deploy/deploy.sh 2>&1 | grep -E "^deploying|SMOKE TEST PASSED|deployed and" | cut -c1-140
  forward; serving

  say "1. Normal traffic: 600 real holdout customers"
  T1=$(now)
  .venv/bin/python -m monitoring.replay --url "http://localhost:$PORT" --rows 600 2>&1 | grep replayed | cut -c25-
  say "2. Drift check on normal traffic"
  .venv/bin/python -m monitoring.drift --since "$T1" --out-dir artifacts/monitoring/normal 2>&1 | grep -E "drift status|ALERT" | cut -c25-
  echo "exit code: ${PIPESTATUS[0]}"

  say "3. Shifted traffic: the same customers 'went quiet' (no orders or browsing in 90 days, last order 150 days later)"
  T2=$(now)
  .venv/bin/python -m monitoring.replay --url "http://localhost:$PORT" --rows 600 --shift went-quiet 2>&1 | grep replayed | cut -c25-
  say "4. Drift check on shifted traffic"
  .venv/bin/python -m monitoring.drift --since "$T2" --out-dir artifacts/monitoring/shifted 2>&1 | grep -E "drift status|ALERT" | cut -c25-
  code=${PIPESTATUS[0]}; echo "exit code: $code"
  cat artifacts/monitoring/shifted/drift.prom | grep -E "alert|prediction_psi|days_since_last_order|orders_90d"

  say "5. Delayed labels: realized performance of the normal-traffic predictions (their 90-day windows have closed)"
  .venv/bin/python -m monitoring.performance --snapshot-date 2026-07-01 --out-dir artifacts/monitoring 2>&1 | grep -E "performance:|model v" | cut -c25-200

  if [[ "$code" == "1" ]]; then
    say "6. Alert ChurnDataDrift -> trigger the retraining job (from CronJob churn-retrain)"
    JOB="retrain-drift-$(date +%s)"
    kubectl -n "$NS" create job "$JOB" --from=cronjob/churn-retrain
    kubectl -n "$NS" wait --for=condition=complete "job/$JOB" --timeout=40m >/dev/null 2>&1 \
      || kubectl -n "$NS" wait --for=condition=failed "job/$JOB" --timeout=1s >/dev/null 2>&1
    kubectl -n "$NS" get job "$JOB"
    kubectl -n "$NS" logs "job/$JOB" | grep -E "INFO training (heuristic|[0-9]_|calibration|final|registered|test pr_auc)|INFO promote|gate exit code" | cut -c25-200

    say "7. Rollout: deploy whatever the gate made 'production'"
    HELM_EXTRA_ARGS="$LOG_ARGS" deploy/deploy.sh 2>&1 | grep -E "^deploying|SMOKE TEST|deployed and" | cut -c1-140
    forward; serving
    helm history churn-api -n "$NS" --max 3
  fi
  pkill -f "port-forward svc/churn-api $PORT" 2>/dev/null
} > "$OUT" 2>&1
echo "wrote $OUT"
