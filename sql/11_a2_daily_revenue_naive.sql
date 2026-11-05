-- 11_a2_daily_revenue_naive.sql
-- A2 (naive): daily revenue per product category, gap-filled, with 7-day moving average
-- and week-over-week growth.
--
-- Window: 2024-07-01 .. 2026-06-30. Revenue = SUM(quantity * unit_price) of completed orders,
-- excluding dq_excluded_customers. Blank categories are reported as 'Unknown'.
-- Gap filling: every (day, category) pair appears, with revenue 0 when nothing sold.
-- ma_7d:      average daily revenue over the current day and the 6 days before it.
-- wow_growth: (last 7 days revenue / previous 7 days revenue) - 1; NULL when the previous week is 0
--             or when fewer than 14 days of history exist (first 13 days of the window).

WITH days AS (
    SELECT generate_series(DATE '2024-07-01', DATE '2026-06-30', INTERVAL '1 day')::date AS revenue_date
),
categories AS (
    SELECT DISTINCT COALESCE(NULLIF(category, ''), 'Unknown') AS category FROM products
),
daily AS (
    SELECT
        o.order_date::date                          AS revenue_date,
        COALESCE(NULLIF(p.category, ''), 'Unknown') AS category,
        SUM(oi.quantity * oi.unit_price)            AS revenue
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    JOIN products p     ON p.product_id = oi.product_id
    WHERE o.status = 'completed'
      AND o.order_date::date BETWEEN DATE '2024-07-01' AND DATE '2026-06-30'
      AND o.customer_id NOT IN (SELECT customer_id FROM dq_excluded_customers)
    GROUP BY 1, 2
),
grid AS (
    SELECT d.revenue_date, c.category, COALESCE(dl.revenue, 0) AS revenue
    FROM days d
    CROSS JOIN categories c
    LEFT JOIN daily dl ON dl.revenue_date = d.revenue_date AND dl.category = c.category
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
