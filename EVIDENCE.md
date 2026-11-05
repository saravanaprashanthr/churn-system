# EVIDENCE

Candidate ID: <to fill>
Approx. hours spent: <to fill>
Environment: macOS 14.5 (Apple Silicon), 8 GB RAM, Docker Desktop 29.9.2 (VM: 8 vCPU / 5.8 GB), PostgreSQL 16, kind v0.33.0
Tools used, including AI assistants, and for what: Claude Code (AI coding assistant) helped with data exploration, writing the Part A SQL and benchmark scripts, and running the benchmarks. <add your own description>

## Summary
| Part | Status (Done / Partial / Skipped) | Key paths | One-line result |
|------|-----------------------------------|-----------|-----------------|
| A. Time-series SQL        | Done | `sql/`, `sql/PERFORMANCE_REPORT.md`, `evidence/sql/` | Warm medians: A1 1,580 → 785 ms, A2 88 → 16 ms, A3a 91 → 33 ms, A3b 701 → 21 ms; all results identical (two-way `EXCEPT ALL`) |
| B. Data quality           | Not started | | |
| C. Data prep pipeline     | Not started | | |
| D. Model development      | Not started | | |
| E. Code quality & tests   | Not started | | |
| F. Kubernetes deployment  | Not started | | |
| G. CI/CD                  | Not started | | |
| H. MLOps & monitoring     | Not started | | |

## Part A: Time-series SQL
- **Claims → evidence**
  - Tested at full scale (orders 500,000, order_items 1,530,225, events 10,000,000) → `evidence/sql/row_counts.txt`
  - Median of 5 runs, cold and warm, before vs after: A1 1,781/1,580 → 797/785 ms; A2 117/88 → 16/16 ms;
    A3a 104/91 → 39/33 ms; A3b 1,089/701 → 280/21 ms → `evidence/sql/timings.md`, `evidence/sql/benchmark_*.csv`
  - Plans for every query before and after → `evidence/sql/<Q>_before_explain.txt`, `evidence/sql/<Q>_after_explain.txt`
  - Optimized queries return identical results (0 rows in both `EXCEPT ALL` directions, A1: 2,533,912 rows) → `evidence/sql/equality_check.txt`
  - BRIN with `minmax_multi` reads 87% fewer blocks than plain `minmax`, which the scattered future-dated events had
    made useless → `evidence/sql/events_window_before_explain.txt`, `evidence/sql/events_window_after_explain.txt`
- **Techniques used and why:** composite/covering B-tree indexes (per-customer lookups); `LATERAL ... LIMIT 1` in place of
  a self-join and correlated subquery; sargable predicates; pre-aggregated summary tables with an idempotent
  incremental refresh; BRIN `minmax_multi` on `events.event_ts` (correlation 0.997; not on `orders.order_date`,
  correlation −0.008); fresh statistics, exact row estimates and `jit = off`. Details: `sql/PERFORMANCE_REPORT.md` §3.
- **Costs / trade-offs:** +407 MB of indexes (+42%); inserting 200k events is ~7× slower (279 → 1,956 ms); the B-tree
  bloated 301 → 394 MB after rolled-back inserts until `REINDEX`; summary tables lag `orders` by up to one refresh
  (daily); A3b gains 33× warm but only 3.9× cold (random I/O) → `evidence/sql/index_cost.txt`, `evidence/sql/refresh_timings.txt`
- **Not done / limitations:** range partitioning (considered; see report §3); A1 is bounded by producing 2.53M output
  rows (~450 ms of 785 ms); `orders.status` is the current status, not the status on the "as of" date (no history table);
  23 high-volume accounts are excluded by rule (`sql/01_dq_excluded_customers.sql`), to be formalised in Part B.
- **How to reproduce:** `sql/PERFORMANCE_REPORT.md` §6.

## Biggest trade-offs I made and what I would do with more time
<to fill at the end>
