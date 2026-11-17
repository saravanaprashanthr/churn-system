"""Request and response schemas for the churn API.

A request carries the feature vector the Part C pipeline publishes for a customer at a cutoff. Order-history
fields are required. Browsing and status fields are optional: a missing value means "no such activity", and
derived fields are computed from their inputs when absent (defaults are listed in ``serving.features``).
Inconsistent input is rejected with HTTP 422 instead of being silently "fixed".
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BATCH = 1000

Count = Annotated[int, Field(ge=0, le=100_000)]
Money = Annotated[float, Field(ge=0, le=1e9)]


class CustomerFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: int | None = Field(
        default=None, description="Echoed back in the response; not a model input."
    )

    # Order history (required): the population has at least one completed order in the last 365 days.
    orders_7d: Count
    orders_30d: Count
    orders_90d: Count
    orders_365d: Annotated[int, Field(ge=1, le=100_000)]
    spend_7d: Money
    spend_30d: Money
    spend_90d: Money
    spend_365d: Money
    orders_prev_30d: Count
    days_since_last_order: Annotated[int, Field(ge=1, le=365)]
    active_days_90d: Annotated[int, Field(ge=0, le=90)]
    tenure_days: Annotated[int, Field(ge=1, le=36_500)]

    # Optional: derived from the fields above when absent.
    order_trend_30d: int | None = None
    avg_order_value_365d: Annotated[float, Field(ge=0)] | None = None

    # Optional: missing means "no such activity".
    cancelled_90d: Count | None = None
    returned_90d: Count | None = None
    views_30d: Count | None = None
    carts_30d: Count | None = None
    checkouts_30d: Count | None = None
    events_30d: Count | None = None
    events_prev_30d: Count | None = None
    event_trend_30d: int | None = None
    sessions_30d: Count | None = None
    days_since_last_event: Annotated[int, Field(ge=1, le=91)] | None = None
    cart_to_view_ratio_30d: Annotated[float, Field(ge=0)] | None = None
    country: Annotated[str, Field(pattern=r"^([A-Z]{2}|unknown)$")] | None = None
    marketing_opt_in: Literal["yes", "no", "unknown"] | None = None

    @model_validator(mode="after")
    def windows_are_nested(self) -> CustomerFeatures:
        orders = (self.orders_7d, self.orders_30d, self.orders_90d, self.orders_365d)
        spend = (self.spend_7d, self.spend_30d, self.spend_90d, self.spend_365d)
        for name, values in (("orders", orders), ("spend", spend)):
            if any(short > long for short, long in zip(values, values[1:], strict=False)):
                raise ValueError(f"{name}_7d <= {name}_30d <= {name}_90d <= {name}_365d must hold")
        if self.active_days_90d > self.orders_90d:
            raise ValueError("active_days_90d cannot exceed orders_90d")
        if (
            self.order_trend_30d is not None
            and self.order_trend_30d != self.orders_30d - self.orders_prev_30d
        ):
            raise ValueError("order_trend_30d must equal orders_30d - orders_prev_30d (or be omitted)")
        return self


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instances: list[CustomerFeatures] = Field(min_length=1, max_length=MAX_BATCH)


class ModelInfo(BaseModel):
    name: str
    version: str
    uri: str
    source: Literal["registry", "bundled"]
    run_id: str | None = None
    git_sha: str | None = None
    data_version: str | None = None
    decision_threshold: float


class Prediction(BaseModel):
    customer_id: int | None
    churn_probability: float
    at_risk: bool


class PredictResponse(BaseModel):
    request_id: str
    model: ModelInfo
    predictions: list[Prediction]
