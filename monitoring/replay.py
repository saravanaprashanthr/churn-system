"""Replay real holdout customers through the API as live traffic, optionally shifted (drift demo).

    python -m monitoring.replay --url http://localhost:8080 --rows 600
    python -m monitoring.replay --url http://localhost:8080 --rows 600 --shift went-quiet

Rows come from the test split of the published datasets; each request carries ``as_of_date`` (the cutoff)
so the delayed-label job can label them later. ``went-quiet`` simulates customers who stopped ordering and
browsing: no orders in the last 90 days, last order 150 days later than reported, no events.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import httpx
import pandas as pd

logger = logging.getLogger("monitoring.replay")

QUIET = {
    "orders_7d": 0, "orders_30d": 0, "orders_90d": 0, "orders_prev_30d": 0, "active_days_90d": 0,
    "spend_7d": 0.0, "spend_30d": 0.0, "spend_90d": 0.0,
}  # fmt: skip
DROP_WHEN_QUIET = (
    "order_trend_30d", "views_30d", "carts_30d", "checkouts_30d", "events_30d", "events_prev_30d",
    "event_trend_30d", "sessions_30d", "days_since_last_event", "cart_to_view_ratio_30d",
)  # fmt: skip


def went_quiet(row: dict) -> dict:
    shifted = {**row, **QUIET, "days_since_last_order": min(365, int(row["days_since_last_order"]) + 150)}
    return {k: v for k, v in shifted.items() if k not in DROP_WHEN_QUIET}


def holdout_rows(n: int, seed: int = 0) -> pd.DataFrame:
    import psycopg

    from training.config import TrainConfig
    from training.data import load_dataset, split

    config = TrainConfig()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        dataset = load_dataset(conn, config.snapshot_date, config.lookback_days, config.split.horizon_days)
    test = split(dataset, config.split)["test"]
    return test.sample(n=min(n, len(test)), random_state=seed)[
        ["customer_id", "cutoff_date", *dataset.feature_columns]
    ]


def replay(url: str, rows: pd.DataFrame, shift: str | None, batch: int = 100) -> dict[str, int]:
    sent, at_risk = 0, 0
    with httpx.Client(base_url=url, timeout=30) as client:
        for cutoff, group in rows.groupby("cutoff_date"):
            records = group.drop(columns=["cutoff_date"]).to_dict(orient="records")
            if shift == "went-quiet":
                records = [went_quiet(r) for r in records]
            for start in range(0, len(records), batch):
                chunk = records[start : start + batch]
                response = client.post("/predict", json={"instances": chunk, "as_of_date": str(cutoff)})
                response.raise_for_status()
                sent += len(chunk)
                at_risk += sum(p["at_risk"] for p in response.json()["predictions"])
    return {"sent": sent, "flagged_at_risk": at_risk}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--rows", type=int, default=600)
    parser.add_argument("--shift", choices=["went-quiet"])
    args = parser.parse_args(argv)
    result = replay(args.url, holdout_rows(args.rows), args.shift)
    logger.info("replayed %s holdout customers (shift=%s): %s", args.rows, args.shift or "none", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
