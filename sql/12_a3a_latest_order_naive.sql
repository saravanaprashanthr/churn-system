-- 12_a3a_latest_order_naive.sql
-- A3a (naive): most recent order and its status per customer, as of date X.
--
-- "As of X" = orders with order_date < X. Ties on order_date are broken by the highest order_id.
-- Excludes dq_excluded_customers. Run with: psql -v as_of=2026-04-01 -f ...
-- Caveat: orders.status is the current status, not the status on date X (no status history table).

\if :{?as_of}
\else
\set as_of '2026-04-01'
\endif

WITH last_dates AS (
    SELECT customer_id, MAX(order_date) AS last_order_date
    FROM orders
    WHERE order_date::date < :'as_of'::date
    GROUP BY customer_id
),
candidates AS (
    SELECT o.customer_id, o.order_id, o.order_date, o.status, o.order_total
    FROM orders o
    JOIN last_dates l ON l.customer_id = o.customer_id AND l.last_order_date = o.order_date
)
SELECT c.customer_id, c.order_id, c.order_date, c.status, c.order_total
FROM candidates c
WHERE c.order_id = (SELECT MAX(c2.order_id) FROM candidates c2 WHERE c2.customer_id = c.customer_id)
  AND c.customer_id NOT IN (SELECT customer_id FROM dq_excluded_customers)
ORDER BY c.customer_id;
