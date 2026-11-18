# Write-up: trade-offs, cuts, and what changes at scale

## What the data decided

Profiling changed the problem before any model existed. **23 accounts place 97% of all orders**: every day for two
years, with ordinary baskets. Bots, test or replayed accounts are the likely explanation. Excluding them by a
behavioral rule (> 1 order per day) leaves **~920 real buyers**. With **98% of customers never buying**, "churn"
only means something for recent buyers. So the population is customers with ≥ 1 completed order in the last
year: ~490 per weekly cutoff, churn ~67%, 31k rows over 65 cutoffs. That is a small, imbalanced-the-other-way
problem, and it shaped the later choices:

- **Simple models win.** Logistic regression beat gradient boosting, and Optuna found the best trees were tiny
  (5 leaves). The final ensemble beats the baseline by only +0.008 PR-AUC on test. I report that as it is.
- **Lift is capped at 1.5** (67% base rate), and the business value of ranking depends on the contact cost: at $5
  per contact the model's top 1.8% earns +$511 on test, while at $2 contacting nearly everyone is as good as the model.
  The threshold is therefore a business setting with a sensitivity table, not a model property.
- **Browsing events add almost nothing**: 97.6% of purchase events belong to customers with no orders. The two
  sources are not linked, so events are weak signals and the label comes only from orders.

## Key trade-offs

| Decision | Chosen | Instead of | Why |
|---|---|---|---|
| DQ engine | 40 custom SQL rules in Postgres | Great Expectations / pandera | 10M events never leave the DB; quarantine is a table downstream SQL can anti-join. pandera is used where data is already a small DataFrame (feature validation). |
| Point-in-time safety | Label window split off before feature code; a test rewrites every row after the cutoff and asserts no feature changes; 90-day embargo between splits | Trusting careful SQL | Leakage is silent. The guard caught a realistic one-character leak in CI (PR #1). |
| Dataset versions | Content hash of rows + parameters | Timestamps or run ids | Re-runs and backfills are idempotent by construction (the demo re-run wrote nothing). |
| Calibration | Only if it improves Brier on later held-out weeks by ≥ 0.001 | Always calibrate | Unconditional isotonic calibration made test Brier worse in round 1 and produced a degenerate threshold. |
| Directional tests | Max increase ≤ 0.005 | Strict monotonicity | The ensemble's tree member has tiny non-monotonic steps (max +0.002); strict tests would block a good model. A tree-only model (+0.014) is still rejected. |
| Model in serving | Pinned version in the ConfigMap + bundled fallback copy | Alias resolved at start-up | A rollback restores image and model together; a registry outage does not stop serving. |
| Prod approval | Manually started workflow for a staged tag | Environment reviewers | Not available on a free private repo; the manual start is an explicit human gate. |

## What I cut, and why

Ordered by value per hour, I kept every ★ item and dropped the ☆ items that need memory the 8 GB laptop does not
have next to Postgres, MLflow and kind: a real KFP install, MinIO, a Prometheus/Grafana stack, load testing and a
canary. CI uses ephemeral kind clusters instead of long-lived staging/prod. Status history and a load timestamp do
not exist in the data, so "late rows" and "status as of the cutoff" are documented limitations, not checks.

## At 100× data volume

- **SQL**: partition `events` and `orders` by month (pruning, cheap retention, small per-partition indexes, which
  also removes the 7× insert penalty of the big B-tree). Keep BRIN on time. Refresh the summary tables incrementally
  (the code exists; at today's size a full rebuild is simpler). Scope DQ rules to the extract window.
- **Pipeline**: the DQ gate runs once per snapshot, not per cutoff; backfills use `ParallelFor` on a real KFP
  cluster; features move to a warehouse or Spark job writing Parquet, with a feature store for online serving.
- **Training**: data no longer fits a laptop; train on sampled or incremental data, rolling-origin validation
  instead of one window, and distributed tuning.
- **Serving**: batch scoring for the weekly campaign (the main use) separate from the online API; the prediction log
  goes to a queue and object storage instead of synchronous Postgres inserts.

## In a multi-team production setting

- **Contracts between teams**: the feature schema (`serving/schemas.py` = `pipeline/transform.py`, enforced by a
  test) becomes a versioned data contract owned with the feature pipeline team. The DQ gate is their CI.
- **Ownership of the threshold**: the retention team owns the cost assumptions; the threshold is recomputed from
  their numbers and recorded with each model version.
- **Promotion rights**: the gate is automatic, but promoting to prod needs the model owner plus an on-call
  approval (Environment reviewers on a paid plan, or ArgoCD watching the `production` alias).
- **Monitoring as a service**: drift and delayed-label jobs publish to the shared Prometheus; alerts route to
  the owning team with the runbook link. Label maturity (90 days) means performance alerts are always late,
  so drift is the early-warning signal.
