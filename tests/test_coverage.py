from datetime import UTC, datetime, timedelta

import pytest

from rebalancer.ingest.lake import LakeWriter
from rebalancer.quality.coverage import compute, load_polls, render

T0 = datetime(2026, 10, 19, 12, 0, 20, tzinfo=UTC)


def _minutes(n, skip=()):
    return [T0 + timedelta(minutes=i) for i in range(n) if i not in skip]


def test_full_coverage():
    c = compute(_minutes(60), failed_polls=0)
    assert c.expected_minutes == 60 and c.covered_minutes == 60 and c.gaps == []


def test_gaps_are_reported_and_short_ones_ignored():
    c = compute(_minutes(100, skip={10, 11} | set(range(50, 60))), failed_polls=2)
    assert c.covered_minutes == 88 and c.expected_minutes == 100
    assert c.gaps == [(T0.replace(second=0) + timedelta(minutes=50), 10)]
    assert not c.passed


def test_overlapping_runs_count_once():
    polls = _minutes(30) + [p + timedelta(seconds=25) for p in _minutes(30)]
    assert compute(polls, 0).covered_minutes == 30


def test_window_is_last_24_hours():
    c = compute(_minutes(30 * 60), failed_polls=0)
    assert c.expected_minutes == 24 * 60 and c.passed
    assert "PASS" in render(c)


def test_short_window_says_so():
    assert "only 1.0 h recorded" in render(compute(_minutes(60), 0))


def test_no_polls_is_an_error():
    with pytest.raises(ValueError):
        compute([], 0)


def test_load_polls_from_lake(tmp_path):
    ms = int(T0.timestamp() * 1000)
    rows = [
        {"polled_at": ms, "feed": "station_status", "ok": True},
        {"polled_at": ms + 60_000, "feed": "station_status", "ok": False},
        {"polled_at": ms, "feed": "station_information", "ok": True},
    ]
    LakeWriter(str(tmp_path)).write("poll_log", rows)
    ok, failed = load_polls(tmp_path)
    assert ok == [T0.replace(microsecond=0)] and failed == 1
