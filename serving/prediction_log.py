"""Write scored customers to Postgres for drift and delayed-label monitoring.

Logging runs after the response is sent (FastAPI background task), so it adds no latency. A logging
failure is counted and logged; it never fails or delays a prediction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import resources
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

Sink = Callable[[list[dict[str, Any]]], None]

INSERT = """
    INSERT INTO ml_prediction_log (request_id, as_of_date, customer_id, model_name, model_version,
                                   model_source, features, churn_probability, at_risk)
    VALUES (%(request_id)s, %(as_of_date)s, %(customer_id)s, %(model_name)s, %(model_version)s,
            %(model_source)s, %(features)s, %(churn_probability)s, %(at_risk)s)
"""


class PostgresSink:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ready = False

    def __call__(self, rows: list[dict[str, Any]]) -> None:
        with psycopg.connect(self.url, connect_timeout=3) as conn:
            if not self._ready:
                conn.execute(resources.files("serving").joinpath("prediction_log.sql").read_text())
                self._ready = True
            with conn.cursor() as cursor:
                cursor.executemany(INSERT, [{**row, "features": Jsonb(row["features"])} for row in rows])
