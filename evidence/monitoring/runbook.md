# Monitoring runbook: what to do when an alert fires

Alert rules: `monitoring/alerts.yaml`. Jobs: `python -m monitoring.drift` (drift), `python -m monitoring.performance`
(delayed labels), CronJob `churn-retrain` (weekly retrain + promotion gate). Deployment and rollback: `deploy/RUNBOOK.md`.

## The loop

```
API logs every scored customer ─► ml_prediction_log
  ├─ drift job (hourly/daily): PSI/KS vs training data ─► ChurnDataDrift ─► retrain Job ─► promotion gate ─► deploy.sh
  └─ performance job (daily): labels for predictions whose 90-day window closed ─► ChurnRealizedPerformanceDrop
CronJob churn-retrain (Mondays 03:00): retrain on the latest published datasets ─► promotion gate
```
Nothing reaches production without the gate (behavior tests + holdout PR-AUC ≥ production + 0.005); nothing is
served without `deploy.sh`'s smoke test.

## Alerts

### ChurnDataDrift (warning; action: retrain)
Prediction PSI ≥ 0.25, or ≥ 3 features with PSI ≥ 0.25, over the drift window.
1. Open `artifacts/monitoring/drift_report.md`: which features moved, and which way did the mean score move?
2. **Is it an upstream bug or the world changing?** Compare the moving features with the feature pipeline: zeros
   or nulls everywhere (e.g. all browsing features at 0) usually mean a broken extract. Check the DQ report of the
   latest pipeline run (`dq_report.md`) and ChurnApiInvalidInputSpike. If it is a bug, fix the pipeline;
   **do not retrain on broken data**.
3. If it is real behavior (seasonality, a campaign, customers going quiet), trigger a retrain:
   `kubectl -n churn create job retrain-$(date +%s) --from=cronjob/churn-retrain`, then follow the Job logs.
4. The gate decides. If the candidate is promoted, run `make deploy` (deploys alias `production`, with smoke test
   and automatic rollback). If it is rejected, the current model stays: note why (`artifacts/promotion/*.json`).
5. Recurring seasonal drift (e.g. November) can be expected; add it to the reference or widen the window
   instead of retraining every year.

### ChurnRealizedPerformanceDrop (critical; action: retrain)
Realized PR-AUC < 0.85 on ≥ 200 predictions whose labels have matured.
1. `artifacts/monitoring/performance_report.md`: which model version and which weeks?
2. Check whether drift was flagged for those weeks. Labels arrive 90 days late, so the damage is old news: look
   at the current drift report too.
3. Retrain as above. If the gate rejects every candidate, the problem is the features or the label, not the
   model: escalate to the data owners.

### ChurnApiNotReady (critical)
A pod has no model (`/ready` 503). `kubectl -n churn logs deploy/churn-api` shows the registry attempts and the
bundled-model error. Usually MLflow is down **and** the image has no bundle, or `MODEL_URI` names a missing
version. Fix MLflow or deploy a known version (`MODEL_VERSION=<n> deploy/deploy.sh`). Unready pods get no traffic.

### ChurnApiServingBundledModel (warning)
Pods started while MLflow was unreachable and serve the model baked into the image. Predictions still work.
Restore MLflow, then `kubectl -n churn rollout restart deploy/churn-api`. Check `/ready` shows `source: registry`.

### ChurnApiHighErrorRate (critical)
5xx > 2% for 5 min. `kubectl -n churn logs deploy/churn-api | grep -v " 200"`. If it started with a deploy,
roll back: `helm rollback churn-api <previous revision> -n churn`.

### ChurnApiHighLatency (warning)
p95 > 300 ms for 10 min. `kubectl -n churn top pods`; the HPA scales 2 → 4 at 70% CPU. Large batches (up to 1,000
customers per request) are the usual cause; ask callers to split them.

### ChurnApiInvalidInputSpike (warning)
More than 10% of requests are rejected with 422. The API is protecting the model; the caller's features are
broken. The 422 body lists the failing fields. Contact the feature-pipeline owner.

### ChurnApiPredictionLogFailing (warning)
Writes to `ml_prediction_log` fail, so drift and performance monitoring are blind (predictions are unaffected).
Check Postgres and the `PREDICTION_LOG_DATABASE_URL` secret.

### ChurnAtRiskShareShift (warning)
The share of customers flagged at risk doubled versus the last 7 days. Treat it like ChurnDataDrift, and tell the
retention team before they send twice as many offers.
