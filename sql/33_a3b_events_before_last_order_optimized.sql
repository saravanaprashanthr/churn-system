-- 33_a3b_events_before_last_order_optimized.sql
-- A3b (optimized): same output as 13_a3b_events_before_last_order_naive.sql.
--
-- Changes vs naive:
--   * Events via LATERAL range lookup on idx_events_customer_ts: reads only the ~4 events
--     per customer inside the 14-day window instead of seq-scanning all 10M events.
--   * Excluded customers are removed before the events lookup (anti-join on orders), not after.
--
-- Why last_orders is a GROUP BY and not a LATERAL LIMIT 1 per customer: driving from
-- customers makes the planner assume all 50k customers have an order (actual: 1,026), so it
-- prices 50k index probes into events above a seq scan and picks the seq scan anyway.
-- GROUP BY orders.customer_id is estimated from n_distinct (~1,056), so the planner correctly
-- chooses ~1k index probes.

WITH last_orders AS (
    SELECT o.customer_id, MAX(o.order_date) AS last_order_date
    FROM orders o
    WHERE o.order_date IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = o.customer_id)
    GROUP BY o.customer_id
)
SELECT l.customer_id, l.last_order_date, e.event_id, e.event_ts, e.event_type, e.product_id, e.session_id
FROM last_orders l
CROSS JOIN LATERAL (
    SELECT ev.event_id, ev.event_ts, ev.event_type, ev.product_id, ev.session_id
    FROM events ev
    WHERE ev.customer_id = l.customer_id
      AND ev.event_ts >= l.last_order_date - INTERVAL '14 days'
      AND ev.event_ts <  l.last_order_date
) e
ORDER BY l.customer_id, e.event_ts, e.event_id;
