"""Container tests: the built image starts and answers, in each registry situation.

    CHURN_API_IMAGE=churn-api:<sha> pytest -m container tests/container

Needs Docker; the registry case also needs MLflow on the host (port 5001).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from serving.smoke import SAMPLE_CUSTOMER

pytestmark = pytest.mark.container

IMAGE = os.environ.get("CHURN_API_IMAGE", "")
UNREACHABLE = "http://host.docker.internal:9"  # nothing listens there: connection refused at once


@pytest.fixture(scope="module")
def docker_client():
    if not IMAGE:
        pytest.skip("CHURN_API_IMAGE is not set")
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    return client


@contextmanager
def running(docker_client, environment: dict[str, str]) -> Iterator[str]:
    container = docker_client.containers.run(
        IMAGE, detach=True, environment={"MODEL_LOAD_ATTEMPTS": "1", **environment},
        ports={"8080/tcp": ("127.0.0.1", None)},
    )  # fmt: skip
    try:
        container.reload()
        port = container.ports["8080/tcp"][0]["HostPort"]
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail(f"container never answered /health; logs:\n{container.logs().decode()[-2000:]}")
        yield url
    finally:
        container.remove(force=True)


def wait_ready(url: str, seconds: float = 60) -> httpx.Response:
    deadline = time.time() + seconds
    response = httpx.get(f"{url}/ready", timeout=2)
    while response.status_code != 200 and time.time() < deadline:
        time.sleep(0.5)
        response = httpx.get(f"{url}/ready", timeout=2)
    return response


def test_registry_reachable_serves_the_registry_model(docker_client):
    with running(docker_client, {"MLFLOW_TRACKING_URI": "http://host.docker.internal:5001",
                                 "MODEL_URI": "models:/churn-90d@production"}) as url:  # fmt: skip
        ready = wait_ready(url)
        assert ready.status_code == 200, ready.text
        assert ready.json()["model"]["source"] == "registry"
        predict = httpx.post(f"{url}/predict", json={"instances": [SAMPLE_CUSTOMER]}, timeout=10)
        assert predict.status_code == 200
        assert 0 <= predict.json()["predictions"][0]["churn_probability"] <= 1


def test_registry_unreachable_falls_back_to_the_bundled_model(docker_client):
    with running(
        docker_client, {"MLFLOW_TRACKING_URI": UNREACHABLE, "MODEL_URI": "models:/churn-90d/3"}
    ) as url:
        ready = wait_ready(url)
        assert ready.status_code == 200, ready.text
        assert ready.json()["model"]["source"] == "bundled"
        body = httpx.post(f"{url}/predict", json={"instances": [SAMPLE_CUSTOMER]}, timeout=10).json()
        assert body["model"]["source"] == "bundled"


def test_no_registry_and_no_bundle_is_alive_but_not_ready(docker_client):
    environment = {"MLFLOW_TRACKING_URI": UNREACHABLE, "MODEL_URI": "models:/churn-90d/3",
                   "BUNDLED_MODEL_DIR": "/nonexistent"}  # fmt: skip
    with running(docker_client, environment) as url:
        assert httpx.get(f"{url}/health", timeout=2).status_code == 200
        assert httpx.get(f"{url}/ready", timeout=2).status_code == 503
        assert (
            httpx.post(f"{url}/predict", json={"instances": [SAMPLE_CUSTOMER]}, timeout=5).status_code == 503
        )


def test_image_runs_as_non_root(docker_client):
    container = docker_client.containers.run(IMAGE, command=["id", "-u"], detach=True)
    try:
        container.wait(timeout=30)
        assert container.logs().decode().strip() == "1000"
    finally:
        container.remove(force=True)
