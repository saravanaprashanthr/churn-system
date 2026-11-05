-- 13_a3b_events_before_last_order_naive.sql
-- A3b (naive): events in the 14 days before each customer's last order.
--
-- Last order = latest dated order of any status. Window = [last_order - 14 days, last_order).
-- Excludes dq_excluded_customers. events has no primary key, so duplicates are kept as-is;
-- correctness checks against the optimized version must use EXCEPT ALL.

WITH last_orders AS (
    SELECT customer_id, MAX(order_date) AS last_order_date
    FROM orders
    WHERE order_date IS NOT NULL
    GROUP BY customer_id
)
SELECT e.customer_id, l.last_order_date, e.event_id, e.event_ts, e.event_type, e.product_id, e.session_id
FROM events e
JOIN last_orders l ON l.customer_id = e.customer_id
WHERE e.event_ts >= l.last_order_date - INTERVAL '14 days'
  AND e.event_ts <  l.last_order_date
  AND e.customer_id NOT IN (SELECT customer_id FROM dq_excluded_customers)
ORDER BY e.customer_id, e.event_ts, e.event_id;
