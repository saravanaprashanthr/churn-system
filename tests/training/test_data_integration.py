"""Loading published dataset versions from Postgres (the Part C -> Part D hand-off)."""

from __future__ import annotations

from datetime import date

import pytest

from pipeline.publish import DatasetParams, publish
from pipeline.transform import FEATURE_COLUMNS
from tests.training.synthetic import synthetic_frame
from training.data import load_dataset

pytestmark = pytest.mark.integration

SNAPSHOT = date(2026, 7, 1)


def publish_cutoff(conn, cutoff: date, seed: int = 0, labeled: bool = True) -> str:
    frame = synthetic_frame([cutoff], rows_per_cutoff=20, seed=seed)
    params = DatasetParams(cutoff, SNAPSHOT, 365, 90, labeled)
    version, _ = publish(conn, frame, params, dq_run_id="dq_test", code_version="test")
    conn.commit()
    return version


def test_load_expands_features_and_uses_one_version_per_cutoff(seeded):
    publish_cutoff(seeded, date(2025, 1, 6))
    old = publish_cutoff(seeded, date(2025, 1, 13), seed=1)
    new = publish_cutoff(seeded, date(2025, 1, 13), seed=2)  # republished later with different content
    assert old != new

    dataset = load_dataset(seeded, SNAPSHOT, 365, 90)
    assert dataset.feature_columns == FEATURE_COLUMNS
    assert len(dataset.frame) == 40
    assert set(dataset.frame.loc[dataset.frame["cutoff_date"] == date(2025, 1, 13), "dataset_version"]) == {
        new
    }
    assert set(dataset.numeric_columns).isdisjoint({"country", "marketing_opt_in"})
    assert dataset.frame["churned"].isin([0, 1]).all()


def test_unlabeled_and_other_parameter_versions_are_ignored(seeded):
    publish_cutoff(seeded, date(2025, 1, 6))
    publish_cutoff(seeded, date(2026, 6, 1), labeled=False)
    dataset = load_dataset(seeded, SNAPSHOT, 365, 90)
    assert set(dataset.frame["cutoff_date"]) == {date(2025, 1, 6)}
    with pytest.raises(LookupError):
        load_dataset(seeded, SNAPSHOT, 180, 90)
