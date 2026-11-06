"""Backfill date generation and argument handling of the local runner (no Docker needed)."""

from __future__ import annotations

from datetime import date

import pytest

from pipeline import run_local


def test_weekly_backfill_includes_both_ends():
    dates = run_local.weekly(date(2025, 1, 6), date(2025, 1, 27))
    assert dates == [date(2025, 1, 6), date(2025, 1, 13), date(2025, 1, 20), date(2025, 1, 27)]


def test_weekly_backfill_of_the_training_range_is_65_mondays():
    dates = run_local.weekly(date(2025, 1, 6), date(2026, 3, 30))
    assert len(dates) == 65
    assert {d.weekday() for d in dates} == {0}


def test_weekly_rejects_reversed_range():
    with pytest.raises(ValueError):
        run_local.weekly(date(2025, 2, 1), date(2025, 1, 1))


def test_cutoffs_and_backfill_are_combined():
    args = run_local.parse_args(
        ["--cutoff", "2026-03-30", "--backfill-start", "2026-03-16", "--backfill-end", "2026-03-23"]
    )
    assert sorted(set(args.cutoff)) == [date(2026, 3, 16), date(2026, 3, 23), date(2026, 3, 30)]


@pytest.mark.parametrize("argv", [[], ["--backfill-start", "2026-03-16"]])
def test_missing_dates_are_a_usage_error(argv):
    with pytest.raises(SystemExit):
        run_local.parse_args(argv)


def test_main_refuses_to_run_without_a_password(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    assert run_local.main(["--cutoff", "2026-03-30"]) == 2
