"""Data quality rule catalog.

Every rule is SQL that runs inside Postgres, so the 10M-row events table is never pulled into
Python. A rule defines:

* ``checked_sql``: one integer, the number of rows (or days) the rule looked at;
* ``violations_sql``: one row per violation, including ``key_columns``;
* ``max_failure_rate``: the share of violations tolerated before the rule fails;
* ``severity``: what a failure does, ``block`` (gate fails) or ``warn`` (logged and reported);
* ``quarantine``: whether violating rows are copied to ``dq_quarantine``.

SQL parameters are passed by psycopg: ``%(as_of)s`` (run date, exclusive upper bound of the data)
and ``%(lookback_days)s`` (days checked by the volume rules). Tolerances for known issues are set
from the profiling of the official data; the reasoning is in ``data_quality/decision_log.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"


class Dimension(StrEnum):
    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    CONSISTENCY = "consistency"
    TIMELINESS = "timeliness"
    VOLUME = "volume"


SQL_PARAMS = frozenset({"as_of", "lookback_days"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    dimension: Dimension
    table: str
    description: str
    severity: Severity
    checked_sql: str
    violations_sql: str
    key_columns: tuple[str, ...]
    max_failure_rate: float = 0.0
    quarantine: bool = False
    excludes_customers: bool = False


def _count(table: str, where: str = "TRUE") -> str:
    return f"SELECT COUNT(*) FROM {table} WHERE {where}"


def _daily_volume_sql(table: str, ts_column: str, condition: str) -> str:
    """Days in [as_of - lookback_days, as_of) whose row count meets ``condition``.

    ``condition`` compares ``n`` (rows that day) with ``median_28d`` (median of the 28 days before).
    Days with no rows count as 0, so a missing day is caught as a drop.
    """
    return f"""
        WITH days AS (
            SELECT d::date AS day
            FROM generate_series(%(as_of)s::date - (%(lookback_days)s + 28), %(as_of)s::date - 1,
                                 INTERVAL '1 day') AS d
        ),
        counts AS (
            SELECT {ts_column}::date AS day, COUNT(*) AS n
            FROM {table}
            WHERE {ts_column} >= %(as_of)s::date - (%(lookback_days)s + 28)
              AND {ts_column} <  %(as_of)s::date
            GROUP BY 1
        ),
        daily AS (
            SELECT days.day, COALESCE(counts.n, 0) AS n
            FROM days LEFT JOIN counts USING (day)
        ),
        scored AS (
            SELECT d.day, d.n,
                   (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY p.n)
                    FROM daily p WHERE p.day BETWEEN d.day - 28 AND d.day - 1) AS median_28d
            FROM daily d
            WHERE d.day >= %(as_of)s::date - %(lookback_days)s
        )
        SELECT day, n, median_28d, ROUND((n / NULLIF(median_28d, 0))::numeric, 2) AS ratio
        FROM scored
        WHERE {condition}
    """


def _freshness_sql(table: str, ts_column: str, max_lag_days: int) -> str:
    """One violation row if the newest row before as_of is older than max_lag_days."""
    return f"""
        SELECT MAX({ts_column}) AS latest, %(as_of)s::date AS as_of
        FROM {table}
        WHERE {ts_column} < %(as_of)s::date
        HAVING MAX({ts_column}) IS NULL
            OR MAX({ts_column}) < %(as_of)s::date - {max_lag_days}
    """


RULES: tuple[Rule, ...] = (
    # ------------------------------------------------------------------ completeness
    Rule(
        rule_id="ORD_ORDER_DATE_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="orders",
        description="Orders must have an order_date; undated orders cannot be placed before or after "
        "a cutoff.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="SELECT * FROM orders WHERE order_date IS NULL",
        key_columns=("order_id",),
        max_failure_rate=0.05,
        quarantine=True,
    ),
    Rule(
        rule_id="ORD_REQUIRED_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="orders",
        description="Orders must have customer_id, status and order_total.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql=(
            "SELECT * FROM orders WHERE customer_id IS NULL OR status IS NULL OR order_total IS NULL"
        ),
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_REQUIRED_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="order_items",
        description="Order items must have order_id, product_id, quantity and unit_price.",
        severity=Severity.BLOCK,
        checked_sql=_count("order_items"),
        violations_sql=(
            "SELECT * FROM order_items WHERE order_id IS NULL OR product_id IS NULL"
            " OR quantity IS NULL OR unit_price IS NULL"
        ),
        key_columns=("order_item_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_REQUIRED_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="events",
        description="Events must have customer_id, event_ts and event_type.",
        severity=Severity.BLOCK,
        checked_sql=_count("events"),
        violations_sql=(
            "SELECT * FROM events WHERE customer_id IS NULL OR event_ts IS NULL OR event_type IS NULL"
        ),
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_PRODUCT_ID_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="events",
        description="Events should name a product. Observed ~2% missing across all event types; "
        "tolerated up to 5%.",
        severity=Severity.WARN,
        checked_sql=_count("events"),
        violations_sql="SELECT * FROM events WHERE product_id IS NULL",
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.05,
    ),
    Rule(
        rule_id="CUS_MARKETING_OPT_IN_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="customers",
        description="marketing_opt_in should be known. Observed ~10% missing; tolerated up to 15%.",
        severity=Severity.WARN,
        checked_sql=_count("customers"),
        violations_sql="SELECT * FROM customers WHERE marketing_opt_in IS NULL",
        key_columns=("customer_id",),
        max_failure_rate=0.15,
    ),
    Rule(
        rule_id="PRD_CATEGORY_NOT_NULL",
        dimension=Dimension.COMPLETENESS,
        table="products",
        description="Products should have a category; missing ones are reported as 'Unknown' downstream.",
        severity=Severity.WARN,
        checked_sql=_count("products"),
        violations_sql="SELECT * FROM products WHERE category IS NULL OR category = ''",
        key_columns=("product_id",),
    ),
    # ------------------------------------------------------------------ uniqueness
    Rule(
        rule_id="ORD_ORDER_ID_UNIQUE",
        dimension=Dimension.UNIQUENESS,
        table="orders",
        description="order_id must be unique (also enforced by the primary key in this database).",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql=(
            "SELECT order_id, COUNT(*) AS copies FROM orders GROUP BY order_id HAVING COUNT(*) > 1"
        ),
        key_columns=("order_id",),
    ),
    Rule(
        rule_id="EVT_DUPLICATE_ROWS",
        dimension=Dimension.UNIQUENESS,
        table="events",
        description="Events should not be repeated as identical rows (events has no primary key).",
        severity=Severity.WARN,
        checked_sql=_count("events"),
        violations_sql="""
            SELECT event_id, customer_id, event_ts, event_type, product_id, session_id, COUNT(*) AS copies
            FROM events
            GROUP BY event_id, customer_id, event_ts, event_type, product_id, session_id
            HAVING COUNT(*) > 1
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.001,
    ),
    Rule(
        rule_id="EVT_EVENT_ID_UNIQUE",
        dimension=Dimension.UNIQUENESS,
        table="events",
        description="event_id should identify one event. Collisions are different events sharing an id; "
        "both are kept and event_id is never used as a key downstream.",
        severity=Severity.WARN,
        checked_sql=_count("events"),
        violations_sql=(
            "SELECT event_id, COUNT(*) AS copies FROM events GROUP BY event_id HAVING COUNT(*) > 1"
        ),
        key_columns=("event_id",),
    ),
    Rule(
        rule_id="CUS_EMAIL_UNIQUE",
        dimension=Dimension.UNIQUENESS,
        table="customers",
        description="An email should belong to one customer. Shared emails are kept: email is not a key "
        "or feature.",
        severity=Severity.WARN,
        checked_sql=_count("customers"),
        violations_sql="""
            SELECT lower(trim(email)) AS email, COUNT(*) AS customers
            FROM customers WHERE email IS NOT NULL
            GROUP BY 1 HAVING COUNT(*) > 1
        """,
        key_columns=("email",),
    ),
    # ------------------------------------------------------------------ validity
    Rule(
        rule_id="ORD_STATUS_VALID",
        dimension=Dimension.VALIDITY,
        table="orders",
        description="status must be one of completed, cancelled, returned, pending (exact spelling).",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="""
            SELECT * FROM orders
            WHERE status IS NOT NULL AND status NOT IN ('completed', 'cancelled', 'returned', 'pending')
        """,
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ORD_TOTAL_RANGE",
        dimension=Dimension.VALIDITY,
        table="orders",
        description="order_total must be > 0 and <= 100,000.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="SELECT * FROM orders WHERE order_total <= 0 OR order_total > 100000",
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_QUANTITY_RANGE",
        dimension=Dimension.VALIDITY,
        table="order_items",
        description="quantity must be between 1 and 100.",
        severity=Severity.BLOCK,
        checked_sql=_count("order_items"),
        violations_sql="SELECT * FROM order_items WHERE quantity < 1 OR quantity > 100",
        key_columns=("order_item_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_UNIT_PRICE_POSITIVE",
        dimension=Dimension.VALIDITY,
        table="order_items",
        description="order item unit_price must be > 0.",
        severity=Severity.BLOCK,
        checked_sql=_count("order_items"),
        violations_sql="SELECT * FROM order_items WHERE unit_price <= 0",
        key_columns=("order_item_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="PRD_UNIT_PRICE_POSITIVE",
        dimension=Dimension.VALIDITY,
        table="products",
        description="product unit_price must be > 0.",
        severity=Severity.BLOCK,
        checked_sql=_count("products"),
        violations_sql="SELECT * FROM products WHERE unit_price IS NULL OR unit_price <= 0",
        key_columns=("product_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_TYPE_VALID",
        dimension=Dimension.VALIDITY,
        table="events",
        description="event_type must be one of view, add_to_cart, checkout_start, purchase.",
        severity=Severity.BLOCK,
        checked_sql=_count("events"),
        violations_sql="""
            SELECT * FROM events
            WHERE event_type IS NOT NULL
              AND event_type NOT IN ('view', 'add_to_cart', 'checkout_start', 'purchase')
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="CUS_SIGNUP_NOT_FUTURE",
        dimension=Dimension.VALIDITY,
        table="customers",
        description="signup_date must be before the run date.",
        severity=Severity.WARN,
        checked_sql=_count("customers"),
        violations_sql="SELECT * FROM customers WHERE signup_date >= %(as_of)s::date",
        key_columns=("customer_id",),
    ),
    Rule(
        rule_id="CUS_EMAIL_FORMAT",
        dimension=Dimension.VALIDITY,
        table="customers",
        description="email should look like local@domain.tld.",
        severity=Severity.WARN,
        checked_sql=_count("customers"),
        violations_sql=r"SELECT * FROM customers WHERE email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'",
        key_columns=("customer_id",),
    ),
    Rule(
        rule_id="CUS_COUNTRY_CODE",
        dimension=Dimension.VALIDITY,
        table="customers",
        description="country should be a two-letter upper-case code.",
        severity=Severity.WARN,
        checked_sql=_count("customers"),
        violations_sql="SELECT * FROM customers WHERE country IS NULL OR country !~ '^[A-Z]{2}$'",
        key_columns=("customer_id",),
    ),
    Rule(
        rule_id="CUS_ORDER_VOLUME_PLAUSIBLE",
        dimension=Dimension.VALIDITY,
        table="customers",
        description="A customer should not average more than 1 order per day over the 730 days before the "
        "run date. Such accounts (bots, test or replayed accounts) are excluded from all downstream use.",
        severity=Severity.WARN,
        checked_sql="""
            SELECT COUNT(DISTINCT customer_id) FROM orders
            WHERE order_date >= %(as_of)s::date - 730 AND order_date < %(as_of)s::date
        """,
        violations_sql="""
            SELECT customer_id, COUNT(*) AS orders_in_window
            FROM orders
            WHERE order_date >= %(as_of)s::date - 730 AND order_date < %(as_of)s::date
            GROUP BY customer_id
            HAVING COUNT(*) > 730
        """,
        key_columns=("customer_id",),
        max_failure_rate=0.05,
        quarantine=True,
        excludes_customers=True,
    ),
    # ------------------------------------------------------------------ referential integrity
    Rule(
        rule_id="ORD_CUSTOMER_EXISTS",
        dimension=Dimension.REFERENTIAL_INTEGRITY,
        table="orders",
        description="orders.customer_id must exist in customers.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="""
            SELECT o.* FROM orders o
            WHERE o.customer_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = o.customer_id)
        """,
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_ORDER_EXISTS",
        dimension=Dimension.REFERENTIAL_INTEGRITY,
        table="order_items",
        description="order_items.order_id must exist in orders.",
        severity=Severity.BLOCK,
        checked_sql=_count("order_items"),
        violations_sql="""
            SELECT i.* FROM order_items i
            WHERE i.order_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = i.order_id)
        """,
        key_columns=("order_item_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_PRODUCT_EXISTS",
        dimension=Dimension.REFERENTIAL_INTEGRITY,
        table="order_items",
        description="order_items.product_id must exist in products.",
        severity=Severity.BLOCK,
        checked_sql=_count("order_items"),
        violations_sql="""
            SELECT i.* FROM order_items i
            WHERE i.product_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM products p WHERE p.product_id = i.product_id)
        """,
        key_columns=("order_item_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_CUSTOMER_EXISTS",
        dimension=Dimension.REFERENTIAL_INTEGRITY,
        table="events",
        description="events.customer_id must exist in customers (not enforced by a foreign key).",
        severity=Severity.BLOCK,
        checked_sql=_count("events"),
        violations_sql="""
            SELECT e.* FROM events e
            WHERE e.customer_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = e.customer_id)
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_PRODUCT_EXISTS",
        dimension=Dimension.REFERENTIAL_INTEGRITY,
        table="events",
        description="events.product_id, when present, should exist in products (not enforced by a "
        "foreign key).",
        severity=Severity.WARN,
        checked_sql=_count("events", "product_id IS NOT NULL"),
        violations_sql="""
            SELECT e.* FROM events e
            WHERE e.product_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM products p WHERE p.product_id = e.product_id)
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.01,
    ),
    # ------------------------------------------------------------------ cross-table consistency
    Rule(
        rule_id="ORD_TOTAL_MATCHES_ITEMS",
        dimension=Dimension.CONSISTENCY,
        table="orders",
        description="order_total must equal SUM(quantity * unit_price) of its items, within 0.01.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="""
            SELECT o.*, s.items_total
            FROM orders o
            JOIN (SELECT order_id, SUM(quantity * unit_price) AS items_total
                  FROM order_items GROUP BY order_id) s USING (order_id)
            WHERE abs(o.order_total - s.items_total) > 0.01
        """,
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ORD_HAS_ITEMS",
        dimension=Dimension.CONSISTENCY,
        table="orders",
        description="Every order must have at least one item.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders"),
        violations_sql="""
            SELECT o.* FROM orders o
            WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)
        """,
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="ITM_PRICE_MATCHES_PRODUCT",
        dimension=Dimension.CONSISTENCY,
        table="order_items",
        description="Item unit_price should equal the product's list price (prices may legitimately change).",
        severity=Severity.WARN,
        checked_sql=_count("order_items"),
        violations_sql="""
            SELECT i.*, p.unit_price AS list_price
            FROM order_items i JOIN products p USING (product_id)
            WHERE i.unit_price <> p.unit_price
        """,
        key_columns=("order_item_id",),
        max_failure_rate=0.05,
    ),
    Rule(
        rule_id="ORD_AFTER_SIGNUP",
        dimension=Dimension.CONSISTENCY,
        table="orders",
        description="Orders should not be dated before the customer's signup_date. Orders are kept "
        "(an order proves the customer existed); signup_date is treated as unreliable.",
        severity=Severity.WARN,
        checked_sql=_count("orders", "order_date IS NOT NULL"),
        violations_sql="""
            SELECT o.order_id, o.customer_id, o.order_date, c.signup_date
            FROM orders o JOIN customers c USING (customer_id)
            WHERE o.order_date::date < c.signup_date
        """,
        key_columns=("order_id",),
    ),
    Rule(
        rule_id="EVT_AFTER_SIGNUP",
        dimension=Dimension.CONSISTENCY,
        table="events",
        description="Events should not be dated before the customer's signup_date (could be pre-signup "
        "browsing linked to the account later). Events are kept.",
        severity=Severity.WARN,
        checked_sql=_count("events"),
        violations_sql="""
            SELECT e.event_id, e.customer_id, e.event_ts, c.signup_date
            FROM events e JOIN customers c USING (customer_id)
            WHERE e.event_ts::date < c.signup_date
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
    ),
    Rule(
        rule_id="EVT_PURCHASE_HAS_ORDERS",
        dimension=Dimension.CONSISTENCY,
        table="events",
        description="A customer with 'purchase' events should have at least one order. When this fails, "
        "events are usable as behaviour signals only; the churn label comes from orders.",
        severity=Severity.WARN,
        checked_sql=_count("events", "event_type = 'purchase'"),
        violations_sql="""
            SELECT e.event_id, e.customer_id, e.event_ts
            FROM events e
            WHERE e.event_type = 'purchase'
              AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = e.customer_id)
        """,
        key_columns=("event_id", "customer_id", "event_ts"),
    ),
    # ------------------------------------------------------------------ timeliness
    Rule(
        rule_id="ORD_FRESHNESS",
        dimension=Dimension.TIMELINESS,
        table="orders",
        description="The newest order before the run date must be at most 2 days old.",
        severity=Severity.BLOCK,
        checked_sql="SELECT 1",
        violations_sql=_freshness_sql("orders", "order_date", max_lag_days=2),
        key_columns=("latest",),
    ),
    Rule(
        rule_id="EVT_FRESHNESS",
        dimension=Dimension.TIMELINESS,
        table="events",
        description="The newest event before the run date must be at most 2 days old.",
        severity=Severity.BLOCK,
        checked_sql="SELECT 1",
        violations_sql=_freshness_sql("events", "event_ts", max_lag_days=2),
        key_columns=("latest",),
    ),
    Rule(
        rule_id="ORD_NOT_FUTURE",
        dimension=Dimension.TIMELINESS,
        table="orders",
        description="Orders must not be dated on or after the run date.",
        severity=Severity.BLOCK,
        checked_sql=_count("orders", "order_date IS NOT NULL"),
        violations_sql="SELECT * FROM orders WHERE order_date >= %(as_of)s::date",
        key_columns=("order_id",),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    Rule(
        rule_id="EVT_NOT_FUTURE",
        dimension=Dimension.TIMELINESS,
        table="events",
        description="Events must not be dated on or after the run date (future-dated rows would leak "
        "activity into features).",
        severity=Severity.BLOCK,
        checked_sql=_count("events"),
        violations_sql="SELECT * FROM events WHERE event_ts >= %(as_of)s::date",
        key_columns=("event_id", "customer_id", "event_ts"),
        max_failure_rate=0.01,
        quarantine=True,
    ),
    # ------------------------------------------------------------------ volume
    Rule(
        rule_id="ORD_DAILY_VOLUME_DROP",
        dimension=Dimension.VOLUME,
        table="orders",
        description="Daily orders must not fall below 50% of the trailing 28-day median "
        "(a drop usually means a broken or partial extract).",
        severity=Severity.BLOCK,
        checked_sql="SELECT %(lookback_days)s::int",
        violations_sql=_daily_volume_sql("orders", "order_date", "n < 0.5 * median_28d"),
        key_columns=("day",),
    ),
    Rule(
        rule_id="ORD_DAILY_VOLUME_SPIKE",
        dimension=Dimension.VOLUME,
        table="orders",
        description="Daily orders above 3x the trailing 28-day median are reported.",
        severity=Severity.WARN,
        checked_sql="SELECT %(lookback_days)s::int",
        violations_sql=_daily_volume_sql("orders", "order_date", "n > 3 * median_28d"),
        key_columns=("day",),
    ),
    Rule(
        rule_id="EVT_DAILY_VOLUME_DROP",
        dimension=Dimension.VOLUME,
        table="events",
        description="Daily events must not fall below 50% of the trailing 28-day median.",
        severity=Severity.BLOCK,
        checked_sql="SELECT %(lookback_days)s::int",
        violations_sql=_daily_volume_sql("events", "event_ts", "n < 0.5 * median_28d"),
        key_columns=("day",),
    ),
    Rule(
        rule_id="EVT_DAILY_VOLUME_SPIKE",
        dimension=Dimension.VOLUME,
        table="events",
        description="Daily events above 3x the trailing 28-day median are reported. 3x, not 2x: Black "
        "Friday week reaches 2.5x every year and is real traffic.",
        severity=Severity.WARN,
        checked_sql="SELECT %(lookback_days)s::int",
        violations_sql=_daily_volume_sql("events", "event_ts", "n > 3 * median_28d"),
        key_columns=("day",),
    ),
)
