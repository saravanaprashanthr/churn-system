-- 03_ml_dataset_tables.sql
-- Versioned training datasets published by the data preparation pipeline (pipeline/).
-- The publish step runs this same DDL (pipeline/ddl.sql, kept identical by a unit test).

-- One row per dataset version. dataset_version is a content hash of the feature rows plus the
-- parameters that produced them, so the same inputs always give the same version (idempotent publish).
CREATE TABLE IF NOT EXISTS ml_dataset_versions (
    dataset_version TEXT PRIMARY KEY,
    content_hash    TEXT        NOT NULL,
    cutoff_date     DATE        NOT NULL,
    snapshot_date   DATE        NOT NULL,
    lookback_days   INT         NOT NULL,
    horizon_days    INT         NOT NULL,
    labeled         BOOLEAN     NOT NULL,
    n_rows          INT         NOT NULL,
    churn_rate      DOUBLE PRECISION,
    feature_columns JSONB       NOT NULL,
    dq_run_id       TEXT        NOT NULL,
    code_version    TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ml_dataset_versions_cutoff ON ml_dataset_versions (cutoff_date);

-- One row per customer per dataset version. Features are stored as JSONB so new feature versions
-- don't need a schema migration; training expands them with the column list from ml_dataset_versions.
CREATE TABLE IF NOT EXISTS ml_customer_features (
    dataset_version TEXT     NOT NULL REFERENCES ml_dataset_versions (dataset_version) ON DELETE CASCADE,
    customer_id     BIGINT   NOT NULL,
    cutoff_date     DATE     NOT NULL,
    features        JSONB    NOT NULL,
    churned         SMALLINT,
    PRIMARY KEY (dataset_version, customer_id)
);
