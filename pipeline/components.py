"""KFP v2 components. Each one is a thin wrapper around a step in this package.

Every component runs in the project image (``PIPELINE_IMAGE``, tagged with the git SHA), which already
contains the pinned dependencies, the kfp executor and the ``data_quality`` / ``pipeline`` packages, so
nothing is pip-installed at run time (``install_kfp_package=False``). The function bodies are copied into
the compiled YAML, so all imports live inside them.

Database access: ``source_db_url`` carries no password. libpq reads it from the ``PGPASSWORD``
environment variable, injected by the runner (local: ``DockerRunner(environment=...)``;
on a cluster: a Kubernetes secret via ``kfp.kubernetes.use_secret_as_env``).
"""

import os

from kfp import dsl
from kfp.dsl import Artifact, Dataset, Input, Output

PIPELINE_IMAGE = os.environ.get("PIPELINE_IMAGE", "churn-pipeline:dev")


def _component(func):
    return dsl.component(base_image=PIPELINE_IMAGE, install_kfp_package=False)(func)


@_component
def extract_op(
    source_db_url: str,
    cutoff_date: str,
    snapshot_date: str,
    lookback_days: int,
    horizon_days: int,
    raw: Output[Dataset],
):
    """Raw customers, orders and daily event counts for one cutoff, as Parquet."""
    import logging
    from datetime import date
    from pathlib import Path

    import psycopg

    from pipeline.extract import ExtractWindow, extract, write_extract

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    window = ExtractWindow(
        date.fromisoformat(cutoff_date), date.fromisoformat(snapshot_date), lookback_days, horizon_days
    )
    with psycopg.connect(source_db_url) as conn:
        frames = extract(conn, window)
    manifest = write_extract(frames, window, Path(raw.path))
    raw.metadata.update({"rows": manifest["rows"], "labeled": manifest["labeled"], **manifest["sql_params"]})


@_component
def dq_gate_op(
    source_db_url: str,
    snapshot_date: str,
    dq_lookback_days: int,
    report: Output[Artifact],
    exclusions: Output[Dataset],
) -> str:
    """Run the data quality gate; fail the pipeline if a block-severity rule fails."""
    import logging
    from datetime import date
    from pathlib import Path

    import psycopg

    from pipeline.dq_step import run_dq_gate

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with psycopg.connect(source_db_url) as conn:
        gate = run_dq_gate(
            conn,
            date.fromisoformat(snapshot_date),
            dq_lookback_days,
            Path(report.path),
            Path(exclusions.path),
        )
    report.metadata.update(
        {
            "run_id": gate.config.run_id,
            "status": "PASS",
            "warnings": [r.rule.rule_id for r in gate.warnings],
            "rows_quarantined": sum(r.quarantined for r in gate.results),
        }
    )
    return gate.config.run_id


@_component
def transform_op(
    raw: Input[Dataset],
    exclusions: Input[Dataset],
    cutoff_date: str,
    snapshot_date: str,
    lookback_days: int,
    horizon_days: int,
    features: Output[Dataset],
):
    """Point-in-time features and the churn label, excluding what the DQ gate quarantined."""
    import logging
    from datetime import date
    from pathlib import Path

    from pipeline.dq_step import read_exclusions
    from pipeline.extract import ExtractWindow, read_extract
    from pipeline.transform import TransformParams, build_features

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    window = ExtractWindow(
        date.fromisoformat(cutoff_date), date.fromisoformat(snapshot_date), lookback_days, horizon_days
    )
    frames = read_extract(Path(raw.path))
    excluded_customers, quarantined_orders, dq_run_id = read_exclusions(Path(exclusions.path))
    table = build_features(
        **frames,
        excluded_customers=excluded_customers,
        quarantined_orders=quarantined_orders,
        params=TransformParams(window.cutoff_date, lookback_days, horizon_days, window.labeled),
    )
    out = Path(features.path)
    out.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out / "features.parquet", index=False)
    features.metadata.update({"rows": len(table), "labeled": window.labeled, "dq_run_id": dq_run_id})
    logging.getLogger("transform").info("built %d feature rows for cutoff %s", len(table), cutoff_date)


@_component
def validate_op(
    features: Input[Dataset],
    cutoff_date: str,
    snapshot_date: str,
    lookback_days: int,
    horizon_days: int,
    min_rows: int,
    validation: Output[Artifact],
):
    """Schema and sanity checks; fail the pipeline instead of publishing a bad table."""
    import json
    import logging
    from datetime import date
    from pathlib import Path

    import pandas as pd

    from pipeline.extract import ExtractWindow
    from pipeline.validate import ValidationParams, validate

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    window = ExtractWindow(
        date.fromisoformat(cutoff_date), date.fromisoformat(snapshot_date), lookback_days, horizon_days
    )
    table = pd.read_parquet(Path(features.path) / "features.parquet")
    report = validate(table, ValidationParams(window.cutoff_date, lookback_days, window.labeled, min_rows))
    Path(validation.path).write_text(json.dumps(report, indent=2))
    validation.metadata.update(report)
    logging.getLogger("validate").info("validation passed: %s", report)


@_component
def publish_op(
    features: Input[Dataset],
    validation: Input[Artifact],
    source_db_url: str,
    dq_run_id: str,
    cutoff_date: str,
    snapshot_date: str,
    lookback_days: int,
    horizon_days: int,
    dataset: Output[Dataset],
) -> str:
    """Write the dataset to Postgres under its content-hash version; idempotent."""
    import logging
    import os
    import shutil
    from datetime import date
    from pathlib import Path

    import pandas as pd
    import psycopg

    from pipeline.extract import ExtractWindow
    from pipeline.publish import DatasetParams, publish

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    window = ExtractWindow(
        date.fromisoformat(cutoff_date), date.fromisoformat(snapshot_date), lookback_days, horizon_days
    )
    table = pd.read_parquet(Path(features.path) / "features.parquet")
    params = DatasetParams(
        window.cutoff_date, window.snapshot_date, lookback_days, horizon_days, window.labeled
    )
    code_version = os.environ.get("CODE_VERSION", "unknown")
    with psycopg.connect(source_db_url) as conn:
        version, created = publish(conn, table, params, dq_run_id, code_version)
    out = Path(dataset.path)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(features.path) / "features.parquet", out / "features.parquet")
    dataset.metadata.update(
        {"dataset_version": version, "created": created, "rows": len(table), "code_version": code_version}
    )
    logging.getLogger("publish").info("dataset_version=%s created=%s", version, created)
    return version
