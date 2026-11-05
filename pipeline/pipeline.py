"""Pipeline definition and compiler.

PIPELINE_IMAGE=churn-pipeline:<git-sha> python -m pipeline.pipeline --output pipeline/pipeline.yaml
"""

import argparse
from pathlib import Path

from kfp import compiler, dsl

from pipeline.components import dq_gate_op, extract_op, publish_op, transform_op, validate_op

DEFAULT_SOURCE_DB_URL = "postgresql://ds_candidate@host.docker.internal:5432/ecommerce"
DEFAULT_SNAPSHOT_DATE = "2026-07-01"


@dsl.pipeline(
    name="churn-dataset-prep",
    description="extract -> data quality gate -> point-in-time features -> validate -> "
    "publish versioned dataset",
)
def churn_dataset_pipeline(
    cutoff_date: str,
    snapshot_date: str = DEFAULT_SNAPSHOT_DATE,
    lookback_days: int = 365,
    horizon_days: int = 90,
    dq_lookback_days: int = 28,
    min_rows: int = 50,
    source_db_url: str = DEFAULT_SOURCE_DB_URL,
) -> str:
    window = {
        "cutoff_date": cutoff_date,
        "snapshot_date": snapshot_date,
        "lookback_days": lookback_days,
        "horizon_days": horizon_days,
    }
    extract = extract_op(source_db_url=source_db_url, **window).set_display_name("extract")
    gate = dq_gate_op(
        source_db_url=source_db_url, snapshot_date=snapshot_date, dq_lookback_days=dq_lookback_days
    ).set_display_name("data-quality-gate")
    gate.after(extract)
    transform = transform_op(
        raw=extract.outputs["raw"], exclusions=gate.outputs["exclusions"], **window
    ).set_display_name("transform")
    validate = validate_op(
        features=transform.outputs["features"], min_rows=min_rows, **window
    ).set_display_name("validate")
    publish = publish_op(
        features=transform.outputs["features"],
        validation=validate.outputs["validation"],
        source_db_url=source_db_url,
        dq_run_id=gate.outputs["Output"],
        **window,
    ).set_display_name("publish")
    for task in (extract, gate, transform, validate, publish):
        task.set_caching_options(
            False
        )  # idempotence comes from the steps; stale cache hits would hide reruns
    return publish.outputs["Output"]


def compile_pipeline(output: Path) -> Path:
    compiler.Compiler().compile(churn_dataset_pipeline, str(output))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, default=Path("pipeline/pipeline.yaml"))
    args = parser.parse_args()
    print(compile_pipeline(args.output))


if __name__ == "__main__":
    main()
