"""Postgres fixtures for the integration tests.

Each test session creates a throwaway schema in the database named by DQ_TEST_DATABASE_URL,
builds the source tables from sql/schema.sql, and drops the schema at the end. The connection's
search_path points only at that schema, so the gate's unqualified table names never reach the
real tables in public.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tests.seed import AS_OF, SEED_DAYS, SEED_SQL

REPO_ROOT = Path(__file__).resolve().parents[1]


def _schema_ddl() -> str:
    lines = (REPO_ROOT / "sql" / "schema.sql").read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().upper().startswith("DROP"))


@pytest.fixture(scope="session")
def db() -> Iterator[psycopg.Connection]:
    url = os.environ.get("DQ_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DQ_TEST_DATABASE_URL is not set")
    schema = f"dq_test_{uuid.uuid4().hex[:8]}"
    conn = psycopg.connect(url)
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    row = conn.execute("SELECT current_schemas(false)").fetchone()
    assert row is not None and row[0] == [schema]
    conn.execute(_schema_ddl())
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.commit()
        conn.close()


@pytest.fixture
def seeded(db: psycopg.Connection) -> psycopg.Connection:
    """Clean, steady data for SEED_DAYS days before AS_OF: passes every rule."""
    db.execute("DROP TABLE IF EXISTS dq_quarantine, dq_rule_results, dq_excluded_customers")
    db.execute("DROP TABLE IF EXISTS ml_customer_features, ml_dataset_versions")
    db.execute("TRUNCATE events, order_items, orders, products, customers")
    for statement in filter(str.strip, SEED_SQL.split(";")):
        db.execute(statement, {"as_of": AS_OF, "days": SEED_DAYS})
    db.commit()
    return db
