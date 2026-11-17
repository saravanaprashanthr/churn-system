"""Manifest validation: helm lint, kubeconform schemas, and the deployment rules we rely on.

Needs the helm and kubeconform binaries (skipped otherwise); CI installs both.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "churn-api"
GOOD = ["--set", "image.tag=b5eca8b", "--set", "model.version=3"]
KUBERNETES_VERSION = "1.33.0"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["helm", *args], capture_output=True, text=True)  # noqa: S603, S607 - fixed CLI


def render(*values: str) -> dict[str, dict]:
    result = helm("template", "churn-api", str(CHART), *values)
    assert result.returncode == 0, result.stderr
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in yaml.safe_load_all(result.stdout) if d}


def test_helm_lint_passes():
    result = helm("lint", str(CHART), "--strict", *GOOD)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_rendered_manifests_match_the_kubernetes_schemas():
    rendered = helm("template", "churn-api", str(CHART), *GOOD).stdout
    result = subprocess.run(  # noqa: S603 - fixed CLI
        ["kubeconform", "-strict", "-summary", "-kubernetes-version", KUBERNETES_VERSION],  # noqa: S607
        input=rendered, capture_output=True, text=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Invalid: 0, Errors: 0" in result.stdout


def test_expected_objects_are_rendered():
    assert set(render(*GOOD)) == {
        "ConfigMap/churn-api", "Service/churn-api", "Deployment/churn-api",
        "HorizontalPodAutoscaler/churn-api", "PodDisruptionBudget/churn-api", "Pod/churn-api-smoke-test",
    }  # fmt: skip


def test_deployment_is_safe_to_roll_out():
    deployment = render(*GOOD)["Deployment/churn-api"]["spec"]
    assert deployment["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    pod = deployment["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == "churn-api:b5eca8b"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert "startupProbe" in container
    assert set(container["resources"]) == {"requests", "limits"}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_model_is_pinned_to_a_version_in_the_configmap():
    data = render(*GOOD)["ConfigMap/churn-api"]["data"]
    assert data["MODEL_URI"] == "models:/churn-90d/3"


def test_smoke_test_hook_checks_the_deployed_model_version():
    pod = render(*GOOD)["Pod/churn-api-smoke-test"]
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    command = pod["spec"]["containers"][0]["command"]
    assert command[command.index("--expect-version") + 1] == "3"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["--set", "model.version=3"], "image.tag must be a git SHA"),
        (["--set", "image.tag=latest", "--set", "model.version=3"], "never empty or latest"),
        (["--set", "image.tag=b5eca8b"], "model.version must be a registry version number"),
        (["--set", "image.tag=b5eca8b", "--set", "model.version=production"], "model.version"),
    ],
    ids=["no-tag", "latest", "no-model", "alias-instead-of-version"],
)
def test_unsafe_values_refuse_to_render(values, message):
    result = helm("template", "churn-api", str(CHART), *values)
    assert result.returncode != 0
    assert message in result.stderr


def test_secret_only_rendered_when_given_and_rolls_the_pods():
    without = render(*GOOD)
    with_secret = render(*GOOD, "--set", "secret.data.MLFLOW_TRACKING_TOKEN=x")
    assert "Secret/churn-api" not in without and "Secret/churn-api" in with_secret
    annotations = lambda r: r["Deployment/churn-api"]["spec"]["template"]["metadata"]["annotations"]  # noqa: E731
    assert annotations(without)["checksum/secret"] != annotations(with_secret)["checksum/secret"]
