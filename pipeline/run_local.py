"""Run the pipeline with the KFP local DockerRunner: one cutoff, or a backfill over several.

    PGPASSWORD=... python -m pipeline.run_local --cutoff 2026-03-30
    PGPASSWORD=... python -m pipeline.run_local --cutoff 2026-03-16 --cutoff 2026-03-23 --cutoff 2026-03-30
    PGPASSWORD=... python -m pipeline.run_local --backfill-start 2025-01-06 --backfill-end 2026-03-30

Each step runs in its own container from PIPELINE_IMAGE. The database password reaches the containers
as the PGPASSWORD environment variable and is never a pipeline parameter.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("run_local")


def weekly(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("backfill end is before start")
    return [start + timedelta(days=7 * i) for i in range((end - start).days // 7 + 1)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cutoff", type=date.fromisoformat, action="append", default=[])
    parser.add_argument("--backfill-start", type=date.fromisoformat)
    parser.add_argument("--backfill-end", type=date.fromisoformat)
    parser.add_argument("--snapshot-date", default="2026-07-01")
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--horizon-days", type=int, default=90)
    parser.add_argument("--pipeline-root", type=Path, default=Path("artifacts/kfp_local"))
    args = parser.parse_args(argv)
    if bool(args.backfill_start) != bool(args.backfill_end):
        parser.error("--backfill-start and --backfill-end go together")
    if args.backfill_start:
        args.cutoff += weekly(args.backfill_start, args.backfill_end)
    if not args.cutoff:
        parser.error("give at least one --cutoff or a backfill range")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    password = os.environ.get("PGPASSWORD")
    if not password:
        logger.error("PGPASSWORD is not set")
        return 2

    from kfp import local

    from pipeline.pipeline import churn_dataset_pipeline

    local.init(
        runner=local.DockerRunner(environment={"PGPASSWORD": password}),
        pipeline_root=str(args.pipeline_root.resolve()),
        raise_on_error=True,
    )
    versions: dict[date, str] = {}
    for cutoff in sorted(set(args.cutoff)):
        logger.info("=== pipeline run: cutoff=%s snapshot=%s", cutoff, args.snapshot_date)
        task = churn_dataset_pipeline(
            cutoff_date=cutoff.isoformat(),
            snapshot_date=args.snapshot_date,
            lookback_days=args.lookback_days,
            horizon_days=args.horizon_days,
        )
        versions[cutoff] = task.output
        logger.info("=== cutoff=%s -> dataset_version=%s", cutoff, task.output)
    for cutoff, version in versions.items():
        print(f"{cutoff}\t{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
