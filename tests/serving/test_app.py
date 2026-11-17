"""API behavior around a fake model: endpoints, errors, readiness, metadata, metrics."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from serving.app import create_app
from serving.model import LoadedModel, ModelLoadError, Settings
from serving.smoke import run as smoke_run
from tests.serving.conftest import INFO, customer, loaded


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True, "model": INFO.model_dump()}


def test_predict_returns_scores_flags_and_model_metadata(client):
    response = client.post(
        "/predict",
        json={"instances": [customer(customer_id=1, days_since_last_order=300), customer(customer_id=2)]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == INFO.model_dump()
    assert body["request_id"] == response.headers["x-request-id"]
    first, second = body["predictions"]
    assert first == {"customer_id": 1, "churn_probability": round(300 / 365, 6), "at_risk": True}
    assert second["customer_id"] == 2 and second["at_risk"] is False  # 12/365 < threshold 0.5


def test_model_receives_the_training_frame(client):
    client.post("/predict", json={"instances": [customer()]})
    model = client.app.state.model_state.model.estimator
    frame = model.frames[-1]
    assert list(frame.columns)[:4] == ["orders_7d", "orders_30d", "orders_90d", "orders_365d"]
    assert "customer_id" not in frame.columns


def test_invalid_input_gives_422_with_details_and_request_id(client):
    response = client.post(
        "/predict", json={"instances": [customer(orders_365d=-1)]}, headers={"x-request-id": "req-1"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["request_id"] == "req-1"
    assert body["error"] == "invalid request"
    assert any("orders_365d" in d["loc"] for d in body["details"])


def test_malformed_json_is_rejected_not_crashing(client):
    response = client.post("/predict", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422


def test_without_a_model_the_api_is_alive_but_not_ready(unready_client):
    assert unready_client.get("/health").status_code == 200
    ready = unready_client.get("/ready")
    assert ready.status_code == 503
    assert "registry attempt 1" in ready.json()["reason"]
    predict = unready_client.post("/predict", json={"instances": [customer()]})
    assert predict.status_code == 503
    assert predict.json()["error"] == "model not loaded"


def test_bundled_fallback_is_visible_in_responses():
    app = create_app(Settings(), loader=lambda settings: loaded(source="bundled"))
    with TestClient(app) as test_client:
        assert (
            test_client.post("/predict", json={"instances": [customer()]}).json()["model"]["source"]
            == "bundled"
        )


def test_background_reload_recovers_when_the_registry_comes_back():
    calls = {"n": 0}

    def flaky(settings: Settings) -> LoadedModel:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelLoadError("registry down")
        return loaded()

    app = create_app(Settings(reload_interval_seconds=0.05), loader=flaky)
    with TestClient(app) as test_client:
        assert test_client.get("/ready").status_code in (200, 503)
        deadline = time.time() + 5
        while test_client.get("/ready").status_code != 200 and time.time() < deadline:
            time.sleep(0.05)
        assert test_client.get("/ready").status_code == 200
        assert calls["n"] >= 2


def test_metrics_count_requests_scores_and_model(client):
    client.post("/predict", json={"instances": [customer(days_since_last_order=300), customer()]})
    client.post("/predict", json={"instances": [customer(orders_365d=-1)]})
    text = client.get("/metrics").text
    assert 'churn_api_requests_total{path="/predict",status="200"} 1.0' in text
    assert 'churn_api_requests_total{path="/predict",status="422"} 1.0' in text
    assert "churn_api_predictions_total 2.0" in text
    assert "churn_api_at_risk_total 1.0" in text
    assert "churn_api_model_ready 1.0" in text
    assert 'churn_api_model_info{name="churn-90d",source="registry",version="7"} 1.0' in text


def test_smoke_check_passes_against_the_app(client):
    summary = smoke_run(client, requests=5, p95_budget_ms=1000, expect_version="7")
    assert summary["model"]["version"] == "7"


def test_smoke_cli_fails_cleanly_when_the_api_is_unreachable():
    from serving.smoke import main

    assert main(["--url", "http://127.0.0.1:9", "--requests", "1", "--timeout", "0.5"]) == 1


def test_scored_customers_are_logged_with_features_and_as_of_date():
    logged: list[dict] = []
    app = create_app(Settings(), loader=lambda settings: loaded(), sink=logged.extend)
    with TestClient(app) as test_client:
        body = {"instances": [customer(customer_id=5)], "as_of_date": "2026-03-30"}
        response = test_client.post("/predict", json=body)
    assert response.status_code == 200
    (row,) = logged
    assert row["customer_id"] == 5 and str(row["as_of_date"]) == "2026-03-30"
    assert row["model_version"] == "7" and row["model_source"] == "registry"
    assert row["features"]["views_30d"] == customer()["views_30d"]
    assert len(row["features"]) == 27  # the complete model input, defaults applied
    assert row["churn_probability"] == response.json()["predictions"][0]["churn_probability"]


def test_a_failing_prediction_log_never_breaks_predictions():
    def broken(rows: list[dict]) -> None:
        raise ConnectionError("database down")

    app = create_app(Settings(), loader=lambda settings: loaded(), sink=broken)
    with TestClient(app) as test_client:
        assert test_client.post("/predict", json={"instances": [customer()]}).status_code == 200
        assert "churn_api_prediction_log_failures_total 1.0" in test_client.get("/metrics").text


def test_packaged_prediction_log_ddl_matches_sql_script():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "serving" / "prediction_log.sql").read_text() == (
        root / "sql" / "05_prediction_log.sql"
    ).read_text()
