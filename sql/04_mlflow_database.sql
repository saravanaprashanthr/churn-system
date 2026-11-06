-- 04_mlflow_database.sql
-- Separate database for the MLflow tracking server's backend store (docker-compose service "mlflow").
-- Run with psql: \gexec executes the CREATE only if the database does not exist yet.

SELECT 'CREATE DATABASE mlflow OWNER ds_candidate'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mlflow')
\gexec
