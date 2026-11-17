"""Download a registered model version (and its metadata) for bundling into the serving image.

    MLFLOW_TRACKING_URI=http://localhost:5001 python -m serving.fetch_model \\
        --uri models:/churn-90d@production --out build/model

The image serves this copy only when the registry is unreachable at startup (see serving/model.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from serving.model import METADATA_FILE, _parse_uri

logger = logging.getLogger("fetch_model")


def fetch(uri: str, out: Path) -> dict:
    import mlflow
    from mlflow import MlflowClient

    name, kind, ref = _parse_uri(uri)
    client = MlflowClient()
    version = (
        client.get_model_version_by_alias(name, ref)
        if kind == "alias"
        else client.get_model_version(name, ref)
    )
    pinned = f"models:/{name}/{version.version}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    mlflow.artifacts.download_artifacts(artifact_uri=pinned, dst_path=str(out / "model"))
    metadata = {"name": name, "version": str(version.version), "uri": pinned, "tags": dict(version.tags)}
    (out / METADATA_FILE).write_text(json.dumps(metadata, indent=2))
    logger.info("bundled %s into %s", pinned, out)
    return metadata


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--uri", required=True, help="models:/<name>/<version> or models:/<name>@<alias>")
    parser.add_argument("--out", type=Path, default=Path("build/model"))
    args = parser.parse_args(argv)
    fetch(args.uri, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
