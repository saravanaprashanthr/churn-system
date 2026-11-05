-- 91_measure_write_overhead.sql
-- Measures insert cost on events and orders with the current set of indexes.
-- Run once before and once after 20_indexes.sql. Everything is rolled back; no data changes.
-- Rows are copies of existing rows with shifted ids (events has no PK; orders ids are offset
-- past the current max so the primary key does not collide).

\timing on

BEGIN;
INSERT INTO events (event_id, customer_id, event_ts, event_type, product_id, session_id)
SELECT event_id + 100000000, customer_id, event_ts, event_type, product_id, session_id
FROM events
LIMIT 200000;
ROLLBACK;

BEGIN;
INSERT INTO orders (order_id, customer_id, order_date, status, order_total)
SELECT order_id + 100000000, customer_id, order_date, status, order_total
FROM orders
LIMIT 50000;
ROLLBACK;

\timing off
