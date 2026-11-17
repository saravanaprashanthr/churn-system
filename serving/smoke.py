"""Post-deploy smoke test: exits non-zero if the deployed API is not fit to serve.

    python -m serving.smoke --url http://churn-api:8080 --requests 50 --p95-budget-ms 300

Checks /health, /ready (model loaded, expected version if given), /predict response schema and values,
that invalid input is rejected with 422, and the p95 latency of single-customer /predict calls.
Runs as the Helm test hook in the cluster, in CI, and from a laptop through a port-forward.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from typing import Any

import httpx

logger = logging.getLogger("smoke")

SAMPLE_CUSTOMER: dict[str, Any] = {
    "customer_id": 1,
    "orders_7d": 0, "orders_30d": 1, "orders_90d": 2, "orders_365d": 4,
    "spend_7d": 0.0, "spend_30d": 80.0, "spend_90d": 150.0, "spend_365d": 400.0,
    "orders_prev_30d": 1, "days_since_last_order": 12, "active_days_90d": 2, "tenure_days": 400,
    "views_30d": 6, "carts_30d": 1, "events_30d": 7, "events_prev_30d": 3, "sessions_30d": 3,
    "days_since_last_event": 2, "country": "US", "marketing_opt_in": "yes",
}  # fmt: skip


class SmokeCheckError(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeCheckError(message)
    logger.info("ok: %s", message)


def run(client: httpx.Client, requests: int, p95_budget_ms: float, expect_version: str | None = None) -> dict:
    """Run every check through ``client`` (a real httpx client, or a FastAPI TestClient in tests)."""
    check(client.get("/health").status_code == 200, "/health returns 200")
    ready = client.get("/ready")
    check(ready.status_code == 200, f"/ready returns 200 (got {ready.status_code}: {ready.text[:200]})")
    model = ready.json()["model"]
    if expect_version:
        check(
            model["version"] == expect_version,
            f"serving model version {expect_version} (got {model['version']})",
        )

    response = client.post(
        "/predict", json={"instances": [SAMPLE_CUSTOMER, {**SAMPLE_CUSTOMER, "customer_id": 2}]}
    )
    check(response.status_code == 200, f"/predict returns 200 (got {response.status_code})")
    body = response.json()
    check(set(body) == {"request_id", "model", "predictions"}, "/predict response has the documented fields")
    check(body["model"]["version"] == model["version"], "/predict reports the loaded model version")
    check(len(body["predictions"]) == 2, "one prediction per instance")
    for prediction in body["predictions"]:
        check(0.0 <= prediction["churn_probability"] <= 1.0, "churn_probability within [0, 1]")
        check(isinstance(prediction["at_risk"], bool), "at_risk is a boolean")

    bad = client.post("/predict", json={"instances": [{**SAMPLE_CUSTOMER, "orders_365d": -1}]})
    check(bad.status_code == 422, f"invalid input is rejected with 422 (got {bad.status_code})")

    latencies = []
    for _ in range(requests):
        started = time.perf_counter()
        client.post("/predict", json={"instances": [SAMPLE_CUSTOMER]}).raise_for_status()
        latencies.append(1000 * (time.perf_counter() - started))
    p95 = statistics.quantiles(latencies, n=20)[-1] if len(latencies) >= 2 else latencies[0]
    check(
        p95 <= p95_budget_ms,
        f"p95 latency {p95:.1f} ms <= budget {p95_budget_ms:.0f} ms over {requests} calls",
    )
    return {"model": model, "p95_ms": round(p95, 1), "median_ms": round(statistics.median(latencies), 1)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per check, not per HTTP call
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--p95-budget-ms", type=float, default=300.0)
    parser.add_argument("--expect-version")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        with httpx.Client(base_url=args.url, timeout=args.timeout) as client:
            summary = run(client, args.requests, args.p95_budget_ms, args.expect_version)
    except (SmokeCheckError, httpx.HTTPError, KeyError, ValueError) as exc:
        logger.error("SMOKE TEST FAILED: %s", exc)
        return 1
    logger.info("SMOKE TEST PASSED: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
