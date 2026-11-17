-- 05_prediction_log.sql
-- Every customer the API scores, for drift monitoring and delayed-label performance (monitoring/).
-- The API runs this same DDL (serving/prediction_log.sql, kept identical by a unit test) when
-- PREDICTION_LOG_DATABASE_URL is set.
CREATE TABLE IF NOT EXISTS ml_prediction_log (
    id                BIGSERIAL PRIMARY KEY,
    logged_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),
    request_id        TEXT             NOT NULL,
    as_of_date        DATE,             -- the cutoff the features describe, when the caller sends it
    customer_id       BIGINT,
    model_name        TEXT             NOT NULL,
    model_version     TEXT             NOT NULL,
    model_source      TEXT             NOT NULL,
    features          JSONB            NOT NULL,
    churn_probability DOUBLE PRECISION NOT NULL,
    at_risk           BOOLEAN          NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_prediction_log_logged_at ON ml_prediction_log (logged_at);
CREATE INDEX IF NOT EXISTS idx_ml_prediction_log_as_of ON ml_prediction_log (as_of_date);
