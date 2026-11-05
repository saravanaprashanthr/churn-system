-- 10_a1_rolling_features_naive.sql
-- A1 (naive): rolling customer features per weekly cutoff, point-in-time correct.
--
-- Cutoffs: 52 Mondays, 2025-07-07 .. 2026-06-29 (last 12 months of data).
-- Population per cutoff: customers who signed up before the cutoff, minus dq_excluded_customers.
-- Only completed orders count, matching the churn label definition.
-- Point-in-time rule: only orders with order_date < cutoff are used (cutoff day itself is "the future").
--
-- Features:
--   orders_7d / 30d / 90d, spend_7d / 30d / 90d  completed orders in [cutoff - N days, cutoff)
--   days_since_last_order                        cutoff - date of last completed order before cutoff (NULL if none)
--   orders_prev_30d                              completed orders in [cutoff - 60, cutoff - 30)
--   order_trend_30d                              orders_30d - orders_prev_30d

WITH cutoffs AS (
    SELECT generate_series(DATE '2025-07-07', DATE '2026-06-29', INTERVAL '7 days')::date AS cutoff_date
),
population AS (
    SELECT c.customer_id, k.cutoff_date
    FROM customers c
    CROSS JOIN cutoffs k
    WHERE c.signup_date < k.cutoff_date
      AND c.customer_id NOT IN (SELECT customer_id FROM dq_excluded_customers)
)
SELECT
    p.customer_id,
    p.cutoff_date,
    COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 7)  AS orders_7d,
    COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 30) AS orders_30d,
    COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 90) AS orders_90d,
    COALESCE(SUM(o.order_total) FILTER (WHERE o.order_date::date >= p.cutoff_date - 7),  0) AS spend_7d,
    COALESCE(SUM(o.order_total) FILTER (WHERE o.order_date::date >= p.cutoff_date - 30), 0) AS spend_30d,
    COALESCE(SUM(o.order_total) FILTER (WHERE o.order_date::date >= p.cutoff_date - 90), 0) AS spend_90d,
    p.cutoff_date - MAX(o.order_date)::date                                     AS days_since_last_order,
    COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 60
                                  AND o.order_date::date <  p.cutoff_date - 30) AS orders_prev_30d,
    COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 30)
  - COUNT(o.order_id)   FILTER (WHERE o.order_date::date >= p.cutoff_date - 60
                                  AND o.order_date::date <  p.cutoff_date - 30) AS order_trend_30d
FROM population p
LEFT JOIN orders o
       ON o.customer_id = p.customer_id
      AND o.status = 'completed'
      AND o.order_date::date < p.cutoff_date
GROUP BY p.customer_id, p.cutoff_date
ORDER BY p.customer_id, p.cutoff_date;
