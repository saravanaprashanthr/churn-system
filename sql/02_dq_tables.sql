-- 02_dq_tables.sql
-- Tables written by the data quality gate (data_quality/). The gate runs this same DDL
-- (data_quality/ddl.sql, kept identical by a unit test) before every run.

-- One row per quarantined source row, per gate run and rule. Rows are never deleted from the
-- source tables; downstream steps exclude them by joining on row_key.
CREATE TABLE IF NOT EXISTS dq_quarantine (
    run_id         TEXT        NOT NULL,
    rule_id        TEXT        NOT NULL,
    table_name     TEXT        NOT NULL,
    row_key        JSONB       NOT NULL,
    row_data       JSONB       NOT NULL,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dq_quarantine_run_rule ON dq_quarantine (run_id, rule_id);

-- One row per gate run and rule: the numbers behind the DQ report.
CREATE TABLE IF NOT EXISTS dq_rule_results (
    run_id           TEXT        NOT NULL,
    rule_id          TEXT        NOT NULL,
    dimension        TEXT        NOT NULL,
    table_name       TEXT        NOT NULL,
    severity         TEXT        NOT NULL,
    rows_checked     BIGINT      NOT NULL,
    violations       BIGINT      NOT NULL,
    failure_rate     DOUBLE PRECISION NOT NULL,
    max_failure_rate DOUBLE PRECISION NOT NULL,
    outcome          TEXT        NOT NULL,
    quarantined      BIGINT      NOT NULL,
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, rule_id)
);

-- Customers excluded from all downstream use, as decided by the latest gate run.
-- Same columns as the Part A bootstrap in 01_dq_excluded_customers.sql.
CREATE TABLE IF NOT EXISTS dq_excluded_customers (
    customer_id      BIGINT PRIMARY KEY,
    orders_in_window BIGINT,
    rule_id          TEXT,
    reason           TEXT,
    flagged_at       TIMESTAMPTZ
);
