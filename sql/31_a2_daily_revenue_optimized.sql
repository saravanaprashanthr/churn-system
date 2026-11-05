-- 31_a2_daily_revenue_optimized.sql
-- A2 (optimized): same output as 11_a2_daily_revenue_naive.sql.
--
-- Changes vs naive: reads daily_category_revenue (pre-aggregated, ~7k rows) instead of
-- joining 500k orders to 1.5M order_items on every run. Gap filling and window functions
-- are unchanged.

WITH days AS (
    SELECT generate_series(DATE '2024-07-01', DATE '2026-06-30', INTERVAL '1 day')::date AS revenue_date
),
categories AS (
    SELECT DISTINCT COALESCE(NULLIF(category, ''), 'Unknown') AS category FROM products
),
grid AS (
    SELECT d.revenue_date, c.category, COALESCE(r.revenue, 0) AS revenue
    FROM days d
    CROSS JOIN categories c
    LEFT JOIN daily_category_revenue r ON r.revenue_day = d.revenue_date AND r.category = c.category
),
weekly AS (
    SELECT
        revenue_date,
        category,
        revenue,
        AVG(revenue) OVER w7 AS ma_7d,
        SUM(revenue) OVER w7 AS rev_7d
    FROM grid
    WINDOW w7 AS (PARTITION BY category ORDER BY revenue_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
)
SELECT
    revenue_date,
    category,
    revenue,
    ROUND(ma_7d, 2) AS ma_7d,
    CASE WHEN revenue_date >= DATE '2024-07-01' + 13 THEN
        ROUND(rev_7d / NULLIF(LAG(rev_7d, 7) OVER (PARTITION BY category ORDER BY revenue_date), 0) - 1, 4)
    END AS wow_growth
FROM weekly
ORDER BY category, revenue_date;
