"""Seed data for the integration tests: SEED_DAYS days of clean, steady activity before AS_OF."""

from datetime import date

AS_OF = date(2026, 3, 1)
SEED_DAYS = 70
ORDERS_PER_DAY = 3  # customers 1, 2 and 3 order once a day
EVENTS_PER_DAY = 5  # customers 1..5 view once a day

SEED_SQL = """
INSERT INTO customers (customer_id, first_name, last_name, email, signup_date, country, marketing_opt_in)
SELECT i, 'First' || i, 'Last' || i, 'customer' || i || '@example.com', DATE '2025-01-01', 'US', TRUE
FROM generate_series(1, 5) AS i;

INSERT INTO products (product_id, product_name, category, unit_price, active)
SELECT i, 'Product ' || i, 'Books', 10.00, TRUE FROM generate_series(1, 3) AS i;

INSERT INTO orders (order_id, customer_id, order_date, status, order_total)
SELECT ROW_NUMBER() OVER (ORDER BY d, c), c, d + TIME '12:00', 'completed', 20.00
FROM generate_series(%(as_of)s::date - %(days)s, %(as_of)s::date - 1, INTERVAL '1 day') AS d,
     generate_series(1, 3) AS c;

INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price)
SELECT order_id, order_id, 1, 2, 10.00 FROM orders;

INSERT INTO events (event_id, customer_id, event_ts, event_type, product_id, session_id)
SELECT ROW_NUMBER() OVER (ORDER BY d, c), c, d + TIME '10:00', 'view', 1, c || '-' || d::date
FROM generate_series(%(as_of)s::date - %(days)s, %(as_of)s::date - 1, INTERVAL '1 day') AS d,
     generate_series(1, 5) AS c;
"""
