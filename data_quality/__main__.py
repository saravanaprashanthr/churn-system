"""Command line entry point: ``python -m data_quality --as-of 2026-07-01``.

Exit codes: 0 gate passed, 1 gate blocked (a block-severity rule failed), 2 configuration error.
The database is read from the DATABASE_URL environment variable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import psycopg

from data_quality.gate import GateConfig, run_gate
from data_quality.report import write_artifacts

logger = logging.getLogger("data_quality")

EXIT_PASS = 0
EXIT_BLOCKED = 1
EXIT_CONFIG_ERROR = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m data_quality", description=__doc__)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        required=True,
        help="run date (YYYY-MM-DD); data on or after it counts as future-dated",
    )
    parser.add_argument("--lookback-days", type=int, default=28, help="days checked by the volume rules")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/dq"),
        help="report folder; the run writes <output-dir>/<run_id>/",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return EXIT_CONFIG_ERROR
    try:
        config = GateConfig(as_of=args.as_of, lookback_days=args.lookback_days)
    except ValueError as exc:
        logger.error("invalid configuration: %s", exc)
        return EXIT_CONFIG_ERROR

    with psycopg.connect(database_url) as conn:
        gate = run_gate(conn, config)
        run_dir = write_artifacts(gate, args.output_dir, conn)
    logger.info("report written to %s", run_dir)
    return EXIT_PASS if gate.passed else EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
