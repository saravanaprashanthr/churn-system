# Data quality gate

40 severity-tagged rules over `customers`, `products`, `orders`, `order_items` and `events`, run as SQL
inside Postgres. Each run quarantines bad rows, stores per-rule results, and writes a report.

| Dimension | Rules | Examples |
|---|---:|---|
| completeness | 7 | undated orders, required keys, missing category / product / opt-in |
| uniqueness | 4 | order_id, exact duplicate events, event_id collisions, shared emails |
| validity | 10 | status and event-type domains, value ranges, implausible order volume |
| referential_integrity | 5 | orders → customers, items → orders/products, events → customers/products |
| consistency | 6 | order_total = Σ items, orders have items, before-signup activity, purchase events vs orders |
| timeliness | 4 | freshness of orders and events, no future-dated rows |
| volume | 4 | daily drops (block) and spikes (warn) vs the trailing 28-day median |

## Run

```bash
export DATABASE_URL=postgresql://ds_candidate:ds_candidate_pw@localhost:5432/ecommerce
.venv/bin/python -m data_quality --as-of 2026-07-01            # exit 0 = pass, 1 = blocked, 2 = config error
```

Writes `artifacts/dq/dq_<YYYYMMDD>/{dq_report.json, dq_report.md, quarantine_sample.csv}` and, in the database,
`dq_quarantine`, `dq_rule_results` and `dq_excluded_customers` (DDL: `sql/02_dq_tables.sql`).
Re-running the same `--as-of` replaces that run's rows, so runs are idempotent.

## Design

- `rules.py`: the catalog. A rule is `checked_sql` (how many rows were looked at), `violations_sql` (one row
  per violation), a severity (`block` / `warn`), a tolerance (`max_failure_rate`) and whether to quarantine.
- `gate.py`: `evaluate()` turns counts into an outcome (pure function); `run_gate()` runs every rule in one
  transaction, writes quarantine rows and results, and rebuilds `dq_excluded_customers`.
- `report.py`: JSON and Markdown report, quarantine sample CSV.
- Why custom SQL instead of Great Expectations / pandera, and every data decision: `decision_log.md`.

## Tests

```bash
DQ_TEST_DATABASE_URL=$DATABASE_URL .venv/bin/pytest tests/data_quality --cov
```

Unit tests check the catalog, the outcome logic, the report and the CLI. Integration tests create a throwaway
schema, seed 70 days of clean data (which must pass every rule), plant one problem per test, and assert the
exact rule, outcome and quarantined keys. Without `DQ_TEST_DATABASE_URL` the integration tests are skipped.
