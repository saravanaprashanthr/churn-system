-- 30_a1_rolling_features_optimized.sql
-- A1 (optimized): same output as 10_a1_rolling_features_naive.sql.
--
-- Changes vs naive:
--   * Reads customer_daily_orders (pre-aggregated, 8,301 rows) instead of joining every
--     customer x cutoff pair to raw orders (2.87M join rows in the naive plan).
--   * Aggregates only for customers that have orders, then LEFT JOINs the result onto the
--     population; 98% of customers never ordered and get zeros without any aggregation work.
--   * Sargable predicates: compares order_day (a DATE column) directly, no casts in filters.
--   * Cutoffs come from an integer generate_series, which the planner estimates exactly (52 rows);
--     the date/interval form is always estimated at 1,000 rows, inflating every estimate above it.

WITH cutoffs AS (
    SELECT DATE '2025-07-07' + 7 * i AS cutoff_date FROM generate_series(0, 51) AS i
),
activity AS (
    SELECT
        d.customer_id,
        k.cutoff_date,
        SUM(d.completed_orders) FILTER (WHERE d.order_day >= k.cutoff_date - 7)  AS orders_7d,
        SUM(d.completed_orders) FILTER (WHERE d.order_day >= k.cutoff_date - 30) AS orders_30d,
        SUM(d.completed_orders) FILTER (WHERE d.order_day >= k.cutoff_date - 90) AS orders_90d,
        SUM(d.completed_spend)  FILTER (WHERE d.order_day >= k.cutoff_date - 7)  AS spend_7d,
        SUM(d.completed_spend)  FILTER (WHERE d.order_day >= k.cutoff_date - 30) AS spend_30d,
        SUM(d.completed_spend)  FILTER (WHERE d.order_day >= k.cutoff_date - 90) AS spend_90d,
        SUM(d.completed_orders) FILTER (WHERE d.order_day >= k.cutoff_date - 60
                                          AND d.order_day <  k.cutoff_date - 30) AS orders_prev_30d,
        MAX(d.order_day)                                                         AS last_order_day
    FROM customer_daily_orders d
    JOIN cutoffs k ON d.order_day < k.cutoff_date
    GROUP BY d.customer_id, k.cutoff_date
)
SELECT
    c.customer_id,
    k.cutoff_date,
    COALESCE(a.orders_7d, 0)                                  AS orders_7d,
    COALESCE(a.orders_30d, 0)                                 AS orders_30d,
    COALESCE(a.orders_90d, 0)                                 AS orders_90d,
    COALESCE(a.spend_7d, 0)                                   AS spend_7d,
    COALESCE(a.spend_30d, 0)                                  AS spend_30d,
    COALESCE(a.spend_90d, 0)                                  AS spend_90d,
    k.cutoff_date - a.last_order_day                          AS days_since_last_order,
    COALESCE(a.orders_prev_30d, 0)                            AS orders_prev_30d,
    COALESCE(a.orders_30d, 0) - COALESCE(a.orders_prev_30d, 0) AS order_trend_30d
FROM customers c
JOIN cutoffs k ON c.signup_date < k.cutoff_date
LEFT JOIN activity a ON a.customer_id = c.customer_id AND a.cutoff_date = k.cutoff_date
WHERE NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = c.customer_id)
ORDER BY c.customer_id, k.cutoff_date;
