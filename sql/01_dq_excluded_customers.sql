-- 01_dq_excluded_customers.sql
-- Customers excluded from everything downstream (features, labels, revenue).
--
-- Rule DQ-ORD-VOLUME: more than 730 orders between 2024-07-01 and 2026-06-30,
-- i.e. more than one order per day on average over the 24-month window.
-- On the official data this flags 23 customers who place ~97% of all orders,
-- order on every day of the window and buy the same basket mix as everyone else
-- (see decision log). The raw orders table is not modified.

DROP TABLE IF EXISTS dq_excluded_customers;

CREATE TABLE dq_excluded_customers AS
SELECT
    customer_id,
    COUNT(*)                                     AS orders_in_window,
    'DQ-ORD-VOLUME'::text                        AS rule_id,
    'more than 730 orders 2024-07-01..2026-06-30 (avg > 1 per day)'::text AS reason,
    now()                                        AS flagged_at
FROM orders
WHERE order_date >= DATE '2024-07-01'
  AND order_date <  DATE '2026-07-01'
GROUP BY customer_id
HAVING COUNT(*) > 730;

ALTER TABLE dq_excluded_customers ADD PRIMARY KEY (customer_id);
ANALYZE dq_excluded_customers;

SELECT COUNT(*) AS excluded_customers, SUM(orders_in_window) AS excluded_orders
FROM dq_excluded_customers;
