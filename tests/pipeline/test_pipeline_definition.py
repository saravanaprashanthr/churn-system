"""The compiled pipeline: steps, wiring, parameters and images."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline import components
from pipeline.pipeline import compile_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def spec(tmp_path_factory) -> dict:
    path = compile_pipeline(tmp_path_factory.mktemp("compiled") / "pipeline.yaml")
    return yaml.safe_load(path.read_text())


def test_pipeline_has_the_five_required_steps_in_order(spec):
    tasks = spec["root"]["dag"]["tasks"]
    assert set(tasks) == {"extract-op", "dq-gate-op", "transform-op", "validate-op", "publish-op"}
    depends = {name: set(task.get("dependentTasks", [])) for name, task in tasks.items()}
    assert depends["extract-op"] == set()
    assert depends["dq-gate-op"] == {"extract-op"}
    assert depends["transform-op"] == {"extract-op", "dq-gate-op"}
    assert depends["validate-op"] == {"transform-op"}
    assert depends["publish-op"] == {"transform-op", "validate-op", "dq-gate-op"}


def test_pipeline_is_parameterised_by_cutoff_lookback_and_source_db(spec):
    params = spec["root"]["inputDefinitions"]["parameters"]
    assert {"cutoff_date", "snapshot_date", "lookback_days", "horizon_days", "source_db_url"} <= set(params)
    assert "defaultValue" not in params["cutoff_date"]  # every run must name its cutoff
    assert params["lookback_days"]["defaultValue"] == 365
    assert "password" not in str(params["source_db_url"].get("defaultValue", "")).lower()
    assert ":@" not in params["source_db_url"]["defaultValue"]  # no password in the URL


def test_every_step_runs_in_the_project_image_without_runtime_installs(spec):
    executors = spec["deploymentSpec"]["executors"].values()
    for executor in executors:
        container = executor["container"]
        assert container["image"] == components.PIPELINE_IMAGE
        assert "pip install" not in " ".join(container.get("command", []) + container.get("args", []))


def test_caching_is_disabled_so_reruns_really_run(spec):
    for task in spec["root"]["dag"]["tasks"].values():
        assert task.get("cachingOptions", {}).get("enableCache", False) is False


def test_pipeline_returns_the_dataset_version(spec):
    assert "Output" in spec["root"]["outputDefinitions"]["parameters"]


def test_committed_yaml_matches_the_code(spec):
    committed = REPO_ROOT / "pipeline" / "pipeline.yaml"
    if not committed.exists():
        pytest.skip("pipeline/pipeline.yaml not compiled yet")
    ours = yaml.safe_load(committed.read_text())
    for name in ("root", "components"):
        assert ours[name] == spec[name], f"pipeline/pipeline.yaml is stale ({name}); recompile it"


def test_packaged_ddl_matches_sql_script():
    assert (REPO_ROOT / "pipeline" / "ddl.sql").read_text() == (
        REPO_ROOT / "sql" / "03_ml_dataset_tables.sql"
    ).read_text()
