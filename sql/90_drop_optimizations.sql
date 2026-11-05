-- 90_drop_optimizations.sql
-- Return to the supplied schema plus dq_excluded_customers, to re-run the "before" benchmark.
-- Re-apply with 20_indexes.sql, 21_summary_tables.sql, 22_planner_settings.sql.

DROP INDEX IF EXISTS idx_orders_customer_date_cov;
DROP INDEX IF EXISTS idx_events_customer_ts;
DROP INDEX IF EXISTS idx_order_items_order;
DROP INDEX IF EXISTS brin_events_event_ts_multi;

DROP FUNCTION IF EXISTS refresh_customer_daily_orders(date, date);
DROP FUNCTION IF EXISTS refresh_daily_category_revenue(date, date);
DROP TABLE IF EXISTS customer_daily_orders;
DROP TABLE IF EXISTS daily_category_revenue;

ALTER DATABASE ecommerce RESET jit;

VACUUM (ANALYZE) orders;
VACUUM (ANALYZE) order_items;
VACUUM (ANALYZE) events;
