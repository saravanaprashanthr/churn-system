-- 32_a3a_latest_order_optimized.sql
-- A3a (optimized): same output as 12_a3a_latest_order_naive.sql.
--
-- Changes vs naive:
--   * LATERAL ... ORDER BY ... LIMIT 1 per customer replaces the MAX() self-join and the
--     correlated tie-break subquery. With idx_orders_customer_date_cov each lookup reads one
--     index entry (index-only scan), so excluded heavy customers' 379k orders are never touched.
--   * Sargable predicate: order_date < X instead of order_date::date < X (same rows, since
--     X is a date, i.e. midnight).

\if :{?as_of}
\else
\set as_of '2026-04-01'
\endif

SELECT c.customer_id, lo.order_id, lo.order_date, lo.status, lo.order_total
FROM customers c
CROSS JOIN LATERAL (
    SELECT o.order_id, o.order_date, o.status, o.order_total
    FROM orders o
    WHERE o.customer_id = c.customer_id
      AND o.order_date < :'as_of'::date
    ORDER BY o.order_date DESC NULLS LAST, o.order_id DESC
    LIMIT 1
) lo
WHERE NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = c.customer_id)
ORDER BY c.customer_id;
