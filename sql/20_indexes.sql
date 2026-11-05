-- 20_indexes.sql
-- Indexes added for Part A. Each one is justified in sql/PERFORMANCE_REPORT.md.

-- Composite covering index for per-customer "latest order" lookups (A3a, A3b).
-- Key order matches ORDER BY order_date DESC NULLS LAST, order_id DESC, so LIMIT 1 per customer
-- reads one index entry. INCLUDE lets A3a answer from the index alone (index-only scan).
CREATE INDEX IF NOT EXISTS idx_orders_customer_date_cov
    ON orders (customer_id, order_date DESC NULLS LAST, order_id DESC)
    INCLUDE (status, order_total);

-- Composite index for "events of customer X between t1 and t2" (A3b).
CREATE INDEX IF NOT EXISTS idx_events_customer_ts
    ON events (customer_id, event_ts);

-- Lets the incremental refresh of daily_category_revenue fetch the items of a few days of orders
-- without scanning all 1.5M order_items.
CREATE INDEX IF NOT EXISTS idx_order_items_order
    ON order_items (order_id)
    INCLUDE (product_id, quantity, unit_price);

-- BRIN on events.event_ts: rows are stored almost in time order (pg_stats correlation 0.997),
-- so a tiny block-range index can skip most of the table for time-window scans
-- (pipeline extract, freshness checks). Not used on orders.order_date: correlation there is -0.008,
-- so every block range would cover the whole date span and BRIN could skip nothing.
--
-- Opclass minmax_multi, not the default minmax: the 9,931 future-dated (2027) events are scattered
-- over 3,154 of the 3,320 block ranges. With plain minmax each such row stretches its range's
-- max to 2027, so a 90-day window query matched almost every range and read 101k of 106k blocks.
-- minmax_multi keeps several min/max intervals per range, so the outliers no longer widen it
-- (13.8k blocks read). Once Part B quarantines those rows out of events, plain minmax would work too.
CREATE INDEX IF NOT EXISTS brin_events_event_ts_multi
    ON events USING brin (event_ts timestamp_minmax_multi_ops) WITH (pages_per_range = 32);

-- Fresh statistics and visibility map, so the planner has correct row estimates
-- and index-only scans do not need heap visits.
VACUUM (ANALYZE) orders;
VACUUM (ANALYZE) order_items;
VACUUM (ANALYZE) events;
