# Part A: Query performance report

All numbers come from the official dataset at full size (customers 50,000 · products 2,000 ·
orders 500,000 · order_items 1,530,225 · events 10,000,000). Raw outputs are in `evidence/sql/`:

| Evidence file | Contents |
|---|---|
| `row_counts.txt` | Row counts after loading: proof of full scale |
| `A1/A2/A3a/A3b_before_explain.txt`, `..._after_explain.txt` | Full `EXPLAIN (ANALYZE, BUFFERS)` per query, before and after |
| `events_window_before_explain.txt`, `..._after_explain.txt` | BRIN A/B (no BRIN vs BRIN) |
| `timings.md` + `benchmark_*.csv` | All runs, medians, cold and warm |
| `equality_check.txt` | Two-way `EXCEPT ALL` results |
| `index_cost.txt` | `\di+` sizes, build times, write overhead before/after, index bloat |
| `refresh_timings.txt` | Summary table refresh times (7-day and full) |

## 1. Setup and method

| Item | Value |
|---|---|
| Machine | macOS laptop, 8 GB RAM; Docker Desktop VM with 8 vCPU / 5.8 GB |
| Database | PostgreSQL 16 (`postgres:16` image), default config: `shared_buffers` 128 MB, `work_mem` 4 MB |
| Timing | `Execution Time` from `EXPLAIN (ANALYZE, TIMING OFF)`: server work only, without sending rows to the client |
| Runs | median of 5 runs per query and cache state (`scripts/benchmark.sh`) |
| Cold cache | before **every** run: Postgres restarted (empty shared buffers) and the Docker VM page cache dropped |
| Warm cache | one discarded warm-up run, then 5 runs back to back |
| Statistics | `VACUUM ANALYZE` on all tables before the baseline, so "before" has fresh statistics too |

**Why server time and not wall-clock time.** A1 returns 2.53M rows. Run through `psql` to
`/dev/null`, the naive A1 took 129 s, but its server execution is ~1.6 s. Most of the
difference is sending and formatting rows on the client, which no SQL change can fix. That's a
consumer concern (Part C reads features per cutoff, not all 52 at once).

### Query semantics shared by every version
- `dq_excluded_customers` (`01_dq_excluded_customers.sql`): 23 customers with more than 730 orders in
  2024-07-01..2026-06-30 are excluded everywhere. The base tables stay full size: queries filter on
  the exclusion table, so every plan still reads the full tables.
- Only `status = 'completed'` orders count for A1 features and A2 revenue (same as the churn label).
- Point-in-time: features use `order_date < cutoff` only.
- A1 cutoffs: 52 Mondays, 2025-07-07..2026-06-29; population = customers who signed up before the cutoff.
- A3a: "as of X" means `order_date < X`; ties are broken by the highest `order_id`.

## 2. Before / after

Same database size for every row. Median of 5 runs, in milliseconds.

| Query | Before: cold | Before: warm | After: cold | After: warm | Speedup cold / warm |
|---|---:|---:|---:|---:|---:|
| A1 rolling features (2,533,912 rows) | 1,781 | 1,580 | 797 | 785 | **2.2× / 2.0×** |
| A2 daily revenue (8,030 rows) | 117 | 88 | 16 | 16 | **7.2× / 5.5×** |
| A3a latest order as of X (974 rows) | 104 | 91 | 39 | 33 | **2.6× / 2.7×** |
| A3b events before last order (3,825 rows) | 1,089 | 701 | 280 | 21 | **3.9× / 32.9×** |
| Events in a 90-day window (BRIN, see T5) | 1,145 | 807 | 565 | 526 | **2.0× / 1.5×** |

- Before = `1x_*_naive.sql` on the supplied schema with default settings.
- After = `3x_*_optimized.sql` after `20_indexes.sql`, `21_summary_tables.sql` and `22_planner_settings.sql`.
- The BRIN row is an A/B on the optimized database: `92_events_time_window_no_brin.sql` (bitmap scans off,
  so a seq scan) vs `14_events_time_window.sql`.

### Where the gain comes from
Naive queries re-run on the optimized database (indexes + `jit = off`, no rewrite):

| Query | Naive, original DB (warm) | Naive, optimized DB (warm) | Optimized query (warm) | Main source of gain |
|---|---:|---:|---:|---|
| A1 | 1,580 | 1,435 | 785 | pre-aggregation + rewrite; JIT off is ~10% |
| A2 | 88 | 50 | 16 | pre-aggregation (summary table) |
| A3a | 91 | 58 | 33 | covering index, then the LATERAL rewrite |
| A3b | 701 | 28 | 21 | **the composite index alone**; the rewrite adds little |

## 3. Techniques and why

### T1: Composite / covering indexes (A3a, A3b)
- `idx_orders_customer_date_cov (customer_id, order_date DESC NULLS LAST, order_id DESC) INCLUDE (status, order_total)`
- `idx_events_customer_ts (customer_id, event_ts)`

**Evidence.** The naive A3b plan seq-scans all 10M events (`Parallel Seq Scan on events`, 3.33M rows per
worker) to return 3,825 rows. With the composite index it does ~1,026 index range probes of about 4 rows
each (`Index Scan using idx_events_customer_ts ... loops=1026`). For A3a, the key order matches
`ORDER BY order_date DESC, order_id DESC`, so `LIMIT 1` reads one entry per customer, and `INCLUDE` makes
it an index-only scan (`Heap Fetches: 0`).

**Cold-cache caveat.** Optimized A3b is 33× faster warm but only 3.9× cold. Events are stored in time order,
not customer order (one customer's events span ~191 heap blocks on average), so each probe is a random read.
Cold, the query reads ~9.4k blocks (4.5k sequential on orders, ~4.9k random on events), and random reads
are slow on Docker Desktop's virtualized disk. A covering index on events would remove the heap reads, but it
would be about as large as the 830 MB table. Not worth it here.

### T2: LATERAL / LIMIT 1 instead of a self-join and correlated subquery (A3a)
The naive A3a computes `MAX(order_date)` per customer, joins back to orders to find the matching rows, then
runs a correlated `MAX(order_id)` subquery to break ties: two full scans of `orders` and a self-join.
The rewrite (`CROSS JOIN LATERAL (... ORDER BY order_date DESC, order_id DESC LIMIT 1)`) needs one index
probe per customer and never touches the 379k orders of excluded customers. 58 → 33 ms on the same indexes.

**What didn't work.** My first A3b rewrite also drove from `customers` with `LATERAL LIMIT 1`. It was
**slower** (1,564 ms). The planner assumed all 49,977 customers have an order (actual: 1,026), so it priced
50k index probes into events above a seq scan, and chose the seq scan. Grouping `orders` by `customer_id`
is estimated from `n_distinct` (~1,056), so the planner picks the index. The query shape decides which
statistics the planner can use.

### T3: Sargable predicates (A1, A3a)
The naive queries filter on `order_date::date < X`. A function on the column hides it from any index on
`order_date`. The optimized queries compare the raw column with a constant (`order_date < X`, the same rows
because X is a date, i.e. midnight), so in A3a the range becomes part of the index condition
(`Index Cond: (customer_id = ...) AND (order_date < ...)`). In A1, the summary table stores a `DATE`
(`order_day`), so no casts are needed. This technique matters mainly because it lets T1 work; it isn't
measured on its own.

### T4: Pre-aggregation with incremental refresh (A1, A2)
- `customer_daily_orders`: one row per customer per day with a completed order (8,301 rows, 0.8 MB).
- `daily_category_revenue`: one row per day per category with sales (9,076 rows, 0.9 MB).

**Evidence.** The naive A1 joins each customer × cutoff pair to the customer's entire completed-order
history: 2.87M join rows, and a sort spilling to disk (`external merge Disk: 11712kB`). The optimized A1
aggregates 8,301 daily rows × cutoffs (346k rows, ~170 ms) for only the customers who have orders, then
LEFT JOINs onto the population. 98% of customers never ordered and get zeros without any aggregation.
The naive A2 joins 500k orders to 1.5M order items on every run; the optimized A2 reads ~7k rows.

**A1's remaining time is producing the output.** Just generating the 2.53M population rows (customers ×
cutoffs, sorted) takes ~450 ms of the 785 ms. A materialized `customer_weekly_features` table would only
move that cost to the refresh. I didn't build one: the pipeline (Part C) asks for one cutoff at a time.

**Refresh strategy.** `refresh_customer_daily_orders(from, to)` / `refresh_daily_category_revenue(from, to)`
delete and rebuild the days in `[from, to)` inside one function call (one transaction), so re-running is
idempotent. The daily job refreshes the last 7 days, which absorbs late-arriving orders and status changes
such as pending → completed.

### T5: BRIN index on `events.event_ts`, with `minmax_multi` (time-window scans)
`brin_events_event_ts_multi USING brin (event_ts timestamp_minmax_multi_ops)`: 920 kB, against 301 MB for the
B-tree on events.

**Why BRIN, and only on events.** BRIN only helps when the physical row order follows the column.
`pg_stats.correlation` is **0.997** for `events.event_ts` but **−0.008** for `orders.order_date` (orders are
stored in random time order), so a BRIN index on orders could skip nothing and I didn't create one. The
access pattern it serves is the pipeline extract and the DQ freshness/volume checks, which read recent events
by time (`14_events_time_window.sql`).

**Dirty data broke the default BRIN.** With the default `minmax` opclass, a 90-day window query read
**101,381 of ~106k blocks** and was *slower* than a seq scan (1,152 vs 770 ms). Cause: the 9,931
future-dated (2027) events are scattered over **3,154 of the 3,320 block ranges**. Each one stretches its
range's max to 2027, so almost every range "might" contain the window. `minmax_multi` keeps several
intervals per range, so the outliers no longer widen it: **13.8k blocks** read (−87%) and 3,370 rows
rechecked instead of 2.75M. The remaining time is mostly the `COUNT(DISTINCT)` sort, not I/O. Once Part B
quarantines the future-dated rows out of `events`, plain `minmax` would work too.

### T6: Planner inputs: fresh statistics, row estimates and JIT
- **Fresh statistics.** `VACUUM (ANALYZE)` after every load and index build (also applied to "before").
- **Row estimates.** `generate_series(date, date, interval)` is always estimated at 1,000 rows, so
  the 52 cutoffs inflated every estimate above them (A1 estimated 16.7M rows, actual 2.5M). The integer
  form `DATE '2025-07-07' + 7 * i FROM generate_series(0, 51)` is estimated exactly.
- **`jit = off`** (`22_planner_settings.sql`, database level). Inflated cost estimates triggered JIT, and on A1
  JIT compilation plus the JIT-compiled plan cost ~1 s: optimized A1 ~1,820 ms with JIT vs ~790 ms without
  (naive ~1,580 vs ~1,400 ms; 2–3 indicative runs each). Every Part A query runs in 15 ms – 2 s, and JIT
  only pays off for long CPU-bound queries. Revisit at 100× volume.

### Considered and not done
- **Range partitioning of `events` by month.** Valid for retention and time pruning, but none of A1–A3
  filters events by time alone (A3b is per customer, and its time window differs per customer), and BRIN
  already prunes the time-window scans at 920 kB. At 100× volume I would partition by month: per-partition
  B-trees stay small (which also fixes the insert overhead below), and old months can be detached cheaply.
- **Covering index on events / materialized A1 table.** See T1 and T4.
- **Raising `work_mem`.** It would remove the small disk spills, but it's a server-wide memory trade-off
  on an 8 GB machine, and it isn't needed after T4.

## 4. Correctness

`scripts/check_equivalence.sh` runs a two-way `EXCEPT ALL` between each naive and optimized query.
It uses `EXCEPT ALL`, not `EXCEPT`, so duplicate rows count too (events has no primary key).
Output: `evidence/sql/equality_check.txt`.

| Pair | Naive rows | Optimized rows | naive − optimized | optimized − naive | Result |
|---|---:|---:|---:|---:|---|
| A1 | 2,533,912 | 2,533,912 | 0 | 0 | identical |
| A2 | 8,030 | 8,030 | 0 | 0 | identical |
| A3a (as of 2026-04-01) | 974 | 974 | 0 | 0 | identical |
| A3a (as of 2025-01-15) | 638 | 638 | 0 | 0 | identical |
| A3b | 3,825 | 3,825 | 0 | 0 | identical |
| Events window, BRIN vs no BRIN | | | | | identical (`diff` of outputs) |

## 5. Costs

### Storage

| Object | Size | Build time |
|---|---:|---:|
| `idx_events_customer_ts` | 301 MB | 6.2 s |
| `idx_order_items_order` | 72 MB | 0.7 s |
| `idx_orders_customer_date_cov` | 31 MB | 0.6 s |
| `brin_events_event_ts_multi` | 0.9 MB | 3.2 s |
| `customer_daily_orders` + PK | 0.8 MB | 0.08 s (full build) |
| `daily_category_revenue` + PK | 0.9 MB | 0.5–0.8 s (full build) |
| **Total added** | **~407 MB** (+42% over the 965 MB of base tables) | |

### Write overhead
Inserts in a rolled-back transaction (`91_measure_write_overhead.sql`), median of 3:

| Insert | Before | After | Factor |
|---|---:|---:|---:|
| 200,000 events | 279 ms | 1,956 ms | **~7×** |
| 50,000 orders | 324 ms | 480 ms | ~1.5× |

The events cost comes almost entirely from the 301 MB `(customer_id, event_ts)` B-tree. New events arrive in
time order but carry random customer IDs, so each insert lands on a random leaf page. For bulk loads, drop and
rebuild the index (6.2 s) or load into a monthly partition with its own small index. The BRIN index costs
almost nothing on insert, because time-ordered appends only touch the last range.

**Bloat.** The write test itself showed a second cost: after 600k rolled-back event inserts the B-tree had grown
from 301 MB to **394 MB** of mostly dead entries, which `VACUUM` does not shrink. `REINDEX` brought it back to
301 MB (5.6 s). A write-heavy events table needs periodic `REINDEX CONCURRENTLY`, or partitions small enough to
rebuild cheaply.

**Environment fix found here.** `VACUUM` on events then failed with `could not resize shared memory segment ...
No space left on device`: Docker gives containers a 64 MB `/dev/shm`, and parallel vacuum/query workers
allocate there. `docker-compose.yml` now sets `shm_size: 1g` (the data volume is unaffected). The benchmarks ran
before this change and did not hit the limit.

### Refresh lag and refresh cost
- The summary tables are only as fresh as their last refresh. With a daily job, A1/A2 can be up to 24 h behind
  `orders`. That's fine for weekly cutoffs, not for real-time dashboards.
- A change older than the 7-day refresh window (very late rows, a status change months later), or a change to
  `dq_excluded_customers`, is missed until a full rebuild.
- Measured (`evidence/sql/refresh_timings.txt`, 3 runs):

| Refresh | Rows | Time |
|---|---:|---:|
| `customer_daily_orders`, last 7 days | 66 | 45–72 ms |
| `customer_daily_orders`, full | 8,301 | 80–87 ms |
| `daily_category_revenue`, last 7 days | 74 | 31–94 ms |
| `daily_category_revenue`, full | 9,076 | 500–800 ms |

The 7-day refresh of `customer_daily_orders` is barely cheaper than the full one, because finding orders by
date alone still means a seq scan of `orders` (no time-ordered index or partition on `orders`). **At the
current size, a full nightly rebuild (<1 s) is simpler and never misses late changes, so that's what I
recommend.** The incremental functions become worth it at 100× volume, together with an index or partition on
`orders.order_date`.

## 6. How to reproduce

```bash
# from a freshly loaded database (SETUP.md)
psql -f /sql/01_dq_excluded_customers.sql
psql -c "VACUUM ANALYZE"
scripts/explain_query.sh sql/10_a1_rolling_features_naive.sql evidence/sql/A1_before_explain.txt   # etc. for 11-13
scripts/benchmark.sh before sql/1[0-3]_*.sql
psql -f /sql/20_indexes.sql -f /sql/21_summary_tables.sql -f /sql/22_planner_settings.sql
scripts/explain_query.sh sql/30_a1_rolling_features_optimized.sql evidence/sql/A1_after_explain.txt  # etc. for 31-33
scripts/benchmark.sh after sql/3[0-3]_*.sql sql/14_events_time_window.sql sql/92_events_time_window_no_brin.sql
scripts/check_equivalence.sh sql/10_a1_rolling_features_naive.sql sql/30_a1_rolling_features_optimized.sql   # etc.
scripts/timings_table.sh > evidence/sql/timings.md
psql -f /sql/90_drop_optimizations.sql   # back to the "before" state
```
(`psql` here means `docker exec -i ds_interview_postgres psql -U ds_candidate -d ecommerce`; the `sql/`
folder is mounted at `/sql` in the container.)
