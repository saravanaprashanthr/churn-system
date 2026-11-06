"""Training configuration: time split, business cost assumptions and seeds.

Time split with embargo. A row's label looks ``horizon_days`` past its cutoff, so a later split may
only start once every label of the earlier split is observable. Otherwise the model would be trained
on outcomes that, at the time the later split is scored, had not happened yet.

    cutoffs:  2025-01-06 .. 2025-05-12 | gap | 2025-08-11 .. 2025-10-06 | gap | 2026-01-05 .. 2026-03-30
              train (19 weekly)        | 90d | valid (9 weekly)         | 90d | test (13 weekly)

Train fits the models, valid selects between iterations and calibrates, test is scored once at the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

SEED = 42


@dataclass(frozen=True)
class SplitConfig:
    train: tuple[date, date] = (date(2025, 1, 6), date(2025, 5, 12))
    valid: tuple[date, date] = (date(2025, 8, 11), date(2025, 10, 6))
    test: tuple[date, date] = (date(2026, 1, 5), date(2026, 3, 30))
    horizon_days: int = 90

    def __post_init__(self) -> None:
        horizon = timedelta(days=self.horizon_days)
        for name, (start, end) in (("train", self.train), ("valid", self.valid), ("test", self.test)):
            if start > end:
                raise ValueError(f"{name} split starts after it ends")
        if self.train[1] + horizon > self.valid[0]:
            raise ValueError("train labels overlap the validation period (embargo too short)")
        if self.valid[1] + horizon > self.test[0]:
            raise ValueError("validation labels overlap the test period (embargo too short)")

    def split_of(self, cutoff: date) -> str | None:
        for name in ("train", "valid", "test"):
            start, end = getattr(self, name)
            if start <= cutoff <= end:
                return name
        return None


@dataclass(frozen=True)
class CostConfig:
    """Retention campaign economics, stated as assumptions (see model_card.md).

    Contacting a customer costs ``contact_cost`` (voucher + handling). A contacted churner is kept with
    probability ``save_rate``, which is worth ``gross_margin`` of their expected spend over the horizon.
    Contacting a customer who would have stayed anyway only costs the contact.
    """

    contact_cost: float = 5.0
    save_rate: float = 0.20
    gross_margin: float = 0.30


@dataclass(frozen=True)
class TrainConfig:
    split: SplitConfig = field(default_factory=SplitConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    snapshot_date: date = date(2026, 7, 1)
    lookback_days: int = 365
    optuna_trials: int = 40
    top_k_fractions: tuple[float, ...] = (0.1, 0.2)
    experiment_name: str = "churn-90d"
    registered_model_name: str = "churn-90d"
    seed: int = SEED
