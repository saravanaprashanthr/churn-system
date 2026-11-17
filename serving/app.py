"""Churn prediction API.

    uvicorn serving.app:create_app --factory --host 0.0.0.0 --port 8080

GET /health   liveness: the process is up (always 200 while it runs)
GET /ready    readiness: 200 with model metadata once a model is loaded, else 503
POST /predict churn probability + at-risk flag per customer, with the model's metadata
GET /metrics  Prometheus metrics
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from serving.features import to_frame
from serving.model import LoadedModel, ModelLoadError, Settings, load_model
from serving.schemas import Prediction, PredictRequest, PredictResponse

logger = logging.getLogger("serving")

Loader = Callable[[Settings], LoadedModel]


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "churn_api_requests_total", "HTTP requests", ["path", "status"], registry=self.registry
        )
        self.latency = Histogram(
            "churn_api_request_latency_seconds", "Request latency", ["path"], registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
        )  # fmt: skip
        self.predictions = Counter("churn_api_predictions_total", "Customers scored", registry=self.registry)
        self.scores = Histogram(
            "churn_api_prediction_score", "Predicted churn probability", registry=self.registry,
            buckets=tuple(i / 10 for i in range(1, 11)),
        )  # fmt: skip
        self.at_risk = Counter("churn_api_at_risk_total", "Customers flagged at risk", registry=self.registry)
        self.ready = Gauge("churn_api_model_ready", "1 when a model is loaded", registry=self.registry)
        self.model_info = Gauge(
            "churn_api_model_info", "Loaded model", ["name", "version", "source"], registry=self.registry
        )


@dataclass
class ModelState:
    model: LoadedModel | None = None
    error: str | None = "model not loaded yet"


def create_app(settings: Settings | None = None, loader: Loader = load_model) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    settings = settings or Settings.from_env()
    state = ModelState()
    metrics = Metrics()
    stop = threading.Event()

    def try_load() -> bool:
        try:
            state.model = loader(settings)
        except (ModelLoadError, ValueError) as exc:
            state.error = str(exc)
            metrics.ready.set(0)
            logger.error("model not loaded: %s", exc)
            return False
        state.error = None
        info = state.model.info
        metrics.ready.set(1)
        metrics.model_info.labels(info.name, info.version, info.source).set(1)
        return True

    def keep_trying() -> None:
        while not stop.wait(settings.reload_interval_seconds):
            if try_load():
                return

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not try_load():
            threading.Thread(target=keep_trying, name="model-reload", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="churn-90d API", version="1.0", lifespan=lifespan)
    app.state.model_state = state
    app.state.metrics = metrics

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        path = (
            request.url.path if request.url.path in {"/health", "/ready", "/predict", "/metrics"} else "other"
        )
        metrics.requests.labels(path, str(response.status_code)).inc()
        metrics.latency.labels(path).observe(elapsed)
        response.headers["x-request-id"] = request_id
        if path not in {"/health", "/metrics"}:
            logger.info("request_id=%s path=%s status=%d latency_ms=%.1f", request_id, path,
                        response.status_code, 1000 * elapsed)  # fmt: skip
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "request_id": request.state.request_id,
                "error": "invalid request",
                "details": errors[:50],
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        if state.model is None:
            return JSONResponse(status_code=503, content={"ready": False, "reason": state.error})
        return JSONResponse(content={"ready": True, "model": state.model.info.model_dump()})

    @app.post("/predict", response_model=PredictResponse)
    def predict(body: PredictRequest, request: Request) -> PredictResponse | JSONResponse:
        request_id = request.state.request_id
        model = state.model
        if model is None:
            return JSONResponse(
                status_code=503,
                content={"request_id": request_id, "error": "model not loaded", "reason": state.error},
            )
        scores = model.predict(to_frame(body.instances))
        threshold = model.info.decision_threshold
        predictions = [
            Prediction(customer_id=item.customer_id, churn_probability=round(float(score), 6),
                       at_risk=bool(score >= threshold))
            for item, score in zip(body.instances, scores, strict=True)
        ]  # fmt: skip
        metrics.predictions.inc(len(predictions))
        metrics.at_risk.inc(sum(p.at_risk for p in predictions))
        for score in scores:
            metrics.scores.observe(float(score))
        return PredictResponse(request_id=request_id, model=model.info, predictions=predictions)

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
