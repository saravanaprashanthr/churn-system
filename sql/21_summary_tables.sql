-- 21_summary_tables.sql
-- Pre-aggregated summary tables with an incremental, idempotent refresh (A1, A2).
--
-- Plain tables plus refresh functions, not MATERIALIZED VIEWs: REFRESH MATERIALIZED VIEW always
-- recomputes the whole view (no incremental refresh in Postgres), while these functions rebuild
-- only the requested day range.
--
-- Both tables contain completed orders only and exclude dq_excluded_customers.
-- Refresh strategy: refresh_*(from, to) deletes and rebuilds the days in [from, to) in one
-- transaction, so re-running it gives the same result. The daily job refreshes the last 7 days
-- to pick up late-arriving orders and status changes (e.g. pending -> completed).
-- A change older than 7 days, or a change to dq_excluded_customers, needs a full rebuild:
--   SELECT refresh_customer_daily_orders('2000-01-01', '2100-01-01');

-- customer_daily_orders: one row per customer per day with at least one completed order.
CREATE TABLE IF NOT EXISTS customer_daily_orders (
    customer_id      BIGINT        NOT NULL,
    order_day        DATE          NOT NULL,
    completed_orders INT           NOT NULL,
    completed_spend  NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (customer_id, order_day)
);

CREATE OR REPLACE FUNCTION refresh_customer_daily_orders(p_from DATE, p_to DATE)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    n_rows BIGINT;
BEGIN
    DELETE FROM customer_daily_orders
    WHERE order_day >= p_from AND order_day < p_to;

    INSERT INTO customer_daily_orders (customer_id, order_day, completed_orders, completed_spend)
    SELECT o.customer_id, o.order_date::date, COUNT(*), SUM(o.order_total)
    FROM orders o
    WHERE o.status = 'completed'
      AND o.order_date >= p_from
      AND o.order_date <  p_to
      AND NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = o.customer_id)
    GROUP BY o.customer_id, o.order_date::date;

    GET DIAGNOSTICS n_rows = ROW_COUNT;
    RETURN n_rows;
END;
$$;

-- daily_category_revenue: one row per day per category with at least one completed sale.
-- Gap filling (zero days) is done at query time, so the table stores only real sales.
CREATE TABLE IF NOT EXISTS daily_category_revenue (
    revenue_day DATE          NOT NULL,
    category    TEXT          NOT NULL,
    revenue     NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (revenue_day, category)
);

CREATE OR REPLACE FUNCTION refresh_daily_category_revenue(p_from DATE, p_to DATE)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    n_rows BIGINT;
BEGIN
    DELETE FROM daily_category_revenue
    WHERE revenue_day >= p_from AND revenue_day < p_to;

    INSERT INTO daily_category_revenue (revenue_day, category, revenue)
    SELECT o.order_date::date,
           COALESCE(NULLIF(p.category, ''), 'Unknown'),
           SUM(oi.quantity * oi.unit_price)
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    JOIN products p     ON p.product_id = oi.product_id
    WHERE o.status = 'completed'
      AND o.order_date >= p_from
      AND o.order_date <  p_to
      AND NOT EXISTS (SELECT 1 FROM dq_excluded_customers x WHERE x.customer_id = o.customer_id)
    GROUP BY 1, 2;

    GET DIAGNOSTICS n_rows = ROW_COUNT;
    RETURN n_rows;
END;
$$;

-- Initial full build.
SELECT refresh_customer_daily_orders(DATE '2000-01-01', DATE '2100-01-01') AS customer_daily_rows;
SELECT refresh_daily_category_revenue(DATE '2000-01-01', DATE '2100-01-01') AS daily_category_rows;
ANALYZE customer_daily_orders;
ANALYZE daily_category_revenue;
