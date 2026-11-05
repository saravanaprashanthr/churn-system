# Data quality decision log

Every issue found while profiling the official data (loaded 2026-11-05), what the gate does about it, and why.
Numbers are from the gate run `dq_20260701` (`evidence/dq/dq_report.md`), with `as_of = 2026-07-01`, the day
after the last order. "Quarantine" means the rows are copied to `dq_quarantine` and excluded downstream. The
source tables are never modified.

| # | Issue | Evidence | Rule (severity, tolerance) | Action | Why |
|---|---|---|---|---|---|
| 1 | **Implausible order volume**: 23 customers average 1.4 to 226 orders a day, every day of the 24 months | 23 of 945 ordering customers hold 379,188 of 391,811 orders (97%). Their baskets match everyone else's (same categories, ~3 lines, qty ≤ 4) | `CUS_ORDER_VOLUME_PLAUSIBLE` (warn, 5%) | Quarantine the customer. `dq_excluded_customers` is rebuilt from it every run; all features, labels and revenue exclude these customers | Not consumer behaviour (bot, test or replayed account): their churn label is meaningless and they would dominate spend features. Detected by behaviour, not a fixed list, so new accounts like this are caught too. >730 orders in 730 days was chosen during profiling: the next customers below it order every other day or less |
| 2 | **Orders without `order_date`** | 10,016 orders (2.0%) | `ORD_ORDER_DATE_NOT_NULL` (block, 5%) | Quarantine | An undated order cannot be placed before or after a cutoff, so it can't safely feed features or labels. 2% is tolerated; above 5% the extract is probably broken, so the gate blocks |
| 3 | **Future-dated events** in Jan to Jun 2027, after a gap with no events in Jul to Dec 2026 | 9,931 events (0.1%), ~1,650 a month | `EVT_NOT_FUTURE` (block, 1%) | Quarantine | Nothing can happen after the extract. The pattern suggests a +1-year timestamp error, but shifting the year back would be a guess. Kept out, they cannot leak "future" activity into features. These rows also broke the default BRIN index (Part A, T5) |
| 4 | **Products without a category** | 97 of 2,000 products, ~4.9% of order lines | `PRD_CATEGORY_NOT_NULL` (warn, 0%) | Keep. Reported as `'Unknown'` in the A2 revenue series | Revenue still adds up to the true total, and the gap stays visible instead of disappearing from category totals |
| 5 | **`event_id` collisions** | 29,883 ids used twice (0.3%). In 29,882 pairs the two rows have different customers, sessions and times (up to 1,056 days apart) | `EVT_EVENT_ID_UNIQUE` (warn, 0%) | Keep both rows. `event_id` is never used as a key downstream | These are two real events sharing an id, not duplicate events. Dropping either would lose real activity. The A3b equality check uses `EXCEPT ALL` for the same reason |
| 6 | **Exact duplicate events** (all columns equal) | 0 | `EVT_DUPLICATE_ROWS` (warn, 0.1%) | None needed now. Downstream event counts would de-duplicate | Checked because events has no primary key, so nothing prevents a double load. It is cheap to keep watching |
| 7 | **Shared emails** across different people (e.g. two different names on `aaron.curry101@…`) | 516 emails on 2+ customers | `CUS_EMAIL_UNIQUE` (warn, 0%) | Keep | Email is neither a key nor a feature. Merging accounts on email would join different people |
| 8 | **Orders dated before the customer's signup** | 14,982 orders (3.1%), all history | `ORD_AFTER_SIGNUP` (warn, 0%) | Keep the orders. Treat `signup_date` as unreliable; tenure features will use `LEAST(signup_date, first order)` | An order is hard evidence the customer existed; a signup date can be reset or back-filled. The opposite choice would delete real purchases |
| 9 | **Events dated before signup** | 1,042,178 events (10.4%), 14,489 customers | `EVT_AFTER_SIGNUP` (warn, 0%) | Keep | Plausibly anonymous browsing linked to the account after signup. Same reasoning as #8 |
| 10 | **`purchase` events don't match orders** | 683,885 of 700,840 purchase events (97.6%) come from customers with no orders at all | `EVT_PURCHASE_HAS_ORDERS` (warn, 0%) | Keep. Events are used as behaviour signals only; the label and all spend features come from `orders` | The two sources are not linked in this dataset. Using purchase events as a proxy for orders would contradict the label definition |
| 11 | **`events.product_id` missing** | 199,962 events (2.0%), spread evenly over all event types | `EVT_PRODUCT_ID_NOT_NULL` (warn, 5%) | Keep | The event still counts as activity; only product-level features lose it. Tolerance set above the observed 2% so the rule fires if it gets worse |
| 12 | **`marketing_opt_in` missing** | 4,993 customers (10.0%) | `CUS_MARKETING_OPT_IN_NOT_NULL` (warn, 15%) | Keep; the model will treat it as a third category ("unknown") | Unknown consent is information in itself. Imputing "false" or "true" would invent it |
| 13 | **Black Friday week**: event spikes of 2.0 to 2.5× the 28-day median, late November in both years | 14 days | `EVT_DAILY_VOLUME_SPIKE` (warn, >3×), `EVT_DAILY_VOLUME_DROP` (block, <0.5×) | No action. Spikes warn above 3×; drops block | A real, recurring traffic pattern: a 2× spike rule would raise a false alarm every November. A drop is the usual sign of a broken or partial extract, so it blocks |
| 14 | **Late-arriving rows** | Cannot be detected: there is no ingestion timestamp, and `order_id` is uncorrelated with `order_date` (corr −0.001) | none possible | Handled by design: the gate and pipeline are idempotent per `as_of`, and the summary tables refresh the last 7 days (Part A) or rebuild fully | Stated as a limitation instead of a check that cannot work. With a load timestamp, a rule "rows loaded after `as_of` but dated before it" would detect them |
| 15 | **`orders.status` reflects today's status, not the status as of a cutoff** | no status history table | none possible | Documented limitation for the model card | An order "completed" today might have been "pending" at the cutoff. Labels look forward from the cutoff, so it mainly affects features. Unfixable without history |

## Checks that pass today and stay as guards

`order_total` = sum of items (all 500,000 orders), item price = product list price, every order has items,
foreign keys from orders/items, event references to customers and products, status/event-type domains,
value ranges, freshness (both tables within 2 days of `as_of`), and daily volume drops. They cost little
(about 25 s for all 40 rules on the full data), and they turn a silent upstream change into a blocked run.
`evidence/dq/blocked_run.log` shows the gate blocking when run 10 days past the end of the data
(freshness and 9 missing days): exit code 1.

## Choices about the gate itself

- **Custom SQL rules, not Great Expectations / pandera / Soda.** Every rule runs inside Postgres, so the
  10M events never leave the database (an 8 GB laptop, and the same reason the A1 features are SQL).
  Quarantine is a SQL table that downstream SQL can anti-join. GE would add a large dependency and
  configuration layer for 40 queries. pandera remains a good fit for validating the small *feature* table
  in the Part C "validate" step, where the data is already a DataFrame.
- **Tolerances.** Known, accepted issues get a tolerance a little above what was observed (e.g. 2% → 5%), so
  the rule passes today and fires if the problem gets worse. Issues with no expected baseline use 0%.
- **Blocked runs still write their results.** Quarantine rows and results are committed even when the gate
  blocks, so the failure can be inspected. Downstream steps don't run after a blocked gate.
- **Changing `dq_excluded_customers` invalidates the Part A summary tables.** The pipeline (Part C) rebuilds
  them after every gate run (under 1 s).
