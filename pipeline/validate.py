"""Validate step: schema and sanity checks on the feature table before it is published.

pandera fits here (unlike the Part B gate): the table is small (~500 rows per cutoff) and already a
DataFrame. All checks run lazily so one failure report lists every problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import pandera.pandas as pa

from pipeline.transform import COUNT_COLUMNS, NO_EVENT_DAYS, SPEND_COLUMNS

MIN_ROWS = 50
CHURN_RATE_BOUNDS = (0.01, 0.99)


class FeatureValidationError(ValueError):
    """Raised when the feature table must not be published."""


@dataclass(frozen=True)
class ValidationParams:
    cutoff_date: date
    lookback_days: int
    labeled: bool
    min_rows: int = MIN_ROWS


def build_schema(params: ValidationParams) -> pa.DataFrameSchema:
    columns: dict[str, pa.Column] = {
        "customer_id": pa.Column("int64", pa.Check.gt(0), unique=True),
        "cutoff_date": pa.Column(object, pa.Check.eq(params.cutoff_date)),
        "days_since_last_order": pa.Column("int64", pa.Check.in_range(1, params.lookback_days)),
        "days_since_last_event": pa.Column("int64", pa.Check.in_range(1, NO_EVENT_DAYS)),
        "order_trend_30d": pa.Column("int64"),
        "event_trend_30d": pa.Column("int64"),
        "avg_order_value_365d": pa.Column("float64", pa.Check.gt(0)),
        "cart_to_view_ratio_30d": pa.Column("float64", pa.Check.ge(0)),
        "tenure_days": pa.Column("int64", pa.Check.ge(1)),
        "country": pa.Column(str, pa.Check.str_matches(r"^([A-Z]{2}|unknown)$")),
        "marketing_opt_in": pa.Column(str, pa.Check.isin(["yes", "no", "unknown"])),
        "orders_365d": pa.Column("int64", pa.Check.ge(1)),  # population: at least one order in the lookback
    }
    for column in COUNT_COLUMNS:
        columns.setdefault(column, pa.Column("int64", pa.Check.ge(0)))
    for column in SPEND_COLUMNS:
        columns[column] = pa.Column("float64", pa.Check.ge(0))
    if params.labeled:
        columns["churned"] = pa.Column("int64", pa.Check.isin([0, 1]))
    else:
        columns["churned"] = pa.Column(
            "Int64", pa.Check(lambda s: s.isna().all(), element_wise=False), nullable=True
        )

    nested_windows = pa.Check(
        lambda df: (
            (df["orders_7d"] <= df["orders_30d"])
            & (df["orders_30d"] <= df["orders_90d"])
            & (df["orders_90d"] <= df["orders_365d"])
            & (df["spend_7d"] <= df["spend_30d"])
            & (df["spend_30d"] <= df["spend_90d"])
            & (df["spend_90d"] <= df["spend_365d"])
        ),
        name="nested_windows",
        error="nested_windows: shorter windows must not exceed longer ones",
    )
    return pa.DataFrameSchema(columns, checks=[nested_windows], strict=True, ordered=False)


def validate(features: pd.DataFrame, params: ValidationParams) -> dict:
    """Return a validation report; raise FeatureValidationError listing every problem found."""
    problems: list[str] = []
    try:
        build_schema(params).validate(features, lazy=True)
    except pa.errors.SchemaErrors as exc:
        cases = exc.failure_cases
        table_level = cases["schema_context"] == "DataFrameSchema"
        for check, group in cases[table_level].groupby("check"):
            if check == "column_in_schema":
                problems.append(f"unexpected columns: {', '.join(map(str, group['failure_case'].unique()))}")
            else:
                problems.append(f"table: {check} ({group['index'].nunique()} failing rows)")
        for (column, check), group in cases[~table_level].groupby(["column", "check"]):
            examples = ", ".join(str(v) for v in group["failure_case"].head(3))
            problems.append(f"{column}: {check} ({len(group)} failing, e.g. {examples})")

    if len(features) < params.min_rows:
        problems.append(f"only {len(features)} rows, expected at least {params.min_rows}")

    churn_rate = None
    if params.labeled and len(features):
        churn_rate = float(features["churned"].mean())
        low, high = CHURN_RATE_BOUNDS
        if not low <= churn_rate <= high:
            problems.append(f"churn rate {churn_rate:.3f} outside [{low}, {high}]: label is degenerate")

    report = {
        "passed": not problems,
        "rows": len(features),
        "churn_rate": churn_rate,
        "labeled": params.labeled,
        "problems": problems,
    }
    if problems:
        raise FeatureValidationError("; ".join(problems))
    return report
