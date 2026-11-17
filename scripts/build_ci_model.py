"""Build a stand-in model bundle for CI, where the real registry and data are not reachable.

    python scripts/build_ci_model.py --out build/model

Trains the baseline pipeline on synthetic data with the real feature schema and writes it in the layout
serving/fetch_model.py produces (model/ + metadata.json, version "0"). CI deploys it with an unreachable
registry, so every CI deploy also exercises the registry -> bundled fallback. It tests the deployment, not
model quality: the real model is gated by training/promote.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import mlflow  # noqa: E402

from serving.model import METADATA_FILE  # noqa: E402
from tests.training.synthetic import synthetic_dataset  # noqa: E402
from training import models  # noqa: E402
from training.data import xy  # noqa: E402


def build(out: Path, git_sha: str) -> None:
    dataset = synthetic_dataset(weeks=8, rows_per_cutoff=100)
    X, y = xy(dataset.frame, dataset.feature_columns)
    model = models.logistic_regression().fit(X, y)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    mlflow.sklearn.save_model(
        model, str(out / "model"), code_paths=[str(REPO_ROOT / "training")],
        skops_trusted_types=list(models.SKOPS_TRUSTED_TYPES),
    )  # fmt: skip
    metadata = {
        "name": "churn-90d",
        "version": "0",
        "uri": "models:/churn-90d/0",
        "tags": {"git_sha": git_sha, "decision_threshold": "0.5", "data_version": "synthetic-ci"},
    }
    (out / METADATA_FILE).write_text(json.dumps(metadata, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=Path("build/model"))
    parser.add_argument("--git-sha", default="ci")
    args = parser.parse_args()
    build(args.out, args.git_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
