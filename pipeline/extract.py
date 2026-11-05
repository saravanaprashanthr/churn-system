"""Extract step: read the raw rows one cutoff needs from Postgres into Parquet files.

The extract is a faithful snapshot: nothing is filtered for data quality here, because the DQ gate
runs after it. Windows (all half-open, ``[start, end)``):

* orders: ``[cutoff - lookback_days, min(cutoff + horizon_days, snapshot_date))``: history for the
  features and the label window, never past the snapshot date;
* events: ``[cutoff - EVENT_LOOKBACK_DAYS, cutoff)``, aggregated to one row per customer, day and
  event type inside Postgres (about 1.3M events become well under 1M rows);
* customers: all rows (50k).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg

logger = logging.getLogger(__name__)

EVENT_LOOKBACK_DAYS = 90

CUSTOMERS_SQL = """
    SELECT customer_id, signup_date, country, marketing_opt_in
    FROM customers
    ORDER BY customer_id
"""

ORDERS_SQL = """
    SELECT order_id, customer_id, order_date, status, order_total
    FROM orders
    WHERE order_date >= %(orders_start)s AND order_date < %(orders_end)s
    ORDER BY order_id
"""

EVENTS_DAILY_SQL = """
    SELECT customer_id, event_ts::date AS day, event_type,
           COUNT(*) AS n_events, COUNT(DISTINCT session_id) AS n_sessions
    FROM events
    WHERE event_ts >= %(events_start)s AND event_ts < %(events_end)s
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
"""


@dataclass(frozen=True)
class ExtractWindow:
    cutoff_date: date
    snapshot_date: date
    lookback_days: int
    horizon_days: int

    def __post_init__(self) -> None:
        if self.lookback_days < 1 or self.horizon_days < 1:
            raise ValueError("lookback_days and horizon_days must be positive")
        if self.cutoff_date > self.snapshot_date:
            raise ValueError("cutoff_date must not be after snapshot_date")

    @property
    def orders_start(self) -> date:
        return self.cutoff_date - timedelta(days=self.lookback_days)

    @property
    def orders_end(self) -> date:
        return min(self.cutoff_date + timedelta(days=self.horizon_days), self.snapshot_date)

    @property
    def events_start(self) -> date:
        return self.cutoff_date - timedelta(days=EVENT_LOOKBACK_DAYS)

    @property
    def events_end(self) -> date:
        return self.cutoff_date

    @property
    def labeled(self) -> bool:
        """The full label window is observable only if it closes on or before the snapshot date."""
        return self.cutoff_date + timedelta(days=self.horizon_days) <= self.snapshot_date

    def sql_params(self) -> dict[str, date]:
        return {
            "orders_start": self.orders_start,
            "orders_end": self.orders_end,
            "events_start": self.events_start,
            "events_end": self.events_end,
        }


def _read(conn: psycopg.Connection, query: str, params: dict[str, date]) -> pd.DataFrame:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        columns = [c.name for c in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=columns)


def extract(conn: psycopg.Connection, window: ExtractWindow) -> dict[str, pd.DataFrame]:
    params = window.sql_params()
    frames = {
        "customers": _read(conn, CUSTOMERS_SQL, params),
        "orders": _read(conn, ORDERS_SQL, params),
        "events_daily": _read(conn, EVENTS_DAILY_SQL, params),
    }
    for name, frame in frames.items():
        logger.info("extracted %s: %d rows", name, len(frame))
    return frames


def write_extract(frames: dict[str, pd.DataFrame], window: ExtractWindow, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(out_dir / f"{name}.parquet", index=False)
    manifest = {
        "window": {k: str(v) for k, v in asdict(window).items()},
        "labeled": window.labeled,
        "sql_params": {k: str(v) for k, v in window.sql_params().items()},
        "rows": {name: len(frame) for name, frame in frames.items()},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def read_extract(in_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        name: pd.read_parquet(in_dir / f"{name}.parquet") for name in ("customers", "orders", "events_daily")
    }
