"""End-to-end check against the running compose stack. Run with `make test-integration`.

Requires `make up` and at least one sink flush (SINK_FLUSH_INTERVAL_S, default 5 min).
"""

import os
import time
from pathlib import Path

import pyarrow.dataset as ds
import pytest

pytestmark = pytest.mark.integration

LAKE = Path(os.environ.get("LAKE_URI", "data/lake"))


def _table(name: str):
    path = LAKE / name
    if not path.exists():
        pytest.fail(f"{path} missing: is the stack running and has the sink flushed?")
    return ds.dataset(path, format="parquet", partitioning="hive").to_table()


def test_lake_has_recent_successful_polls():
    log = _table("poll_log").to_pylist()
    status_polls = [r for r in log if r["feed"] == "station_status"]
    assert status_polls, "no station_status polls recorded"
    assert all(r["ok"] for r in status_polls[-10:])
    newest = max(r["polled_at"] for r in status_polls).timestamp()
    assert time.time() - newest < 15 * 60, "newest poll is older than the 15 min SLA"


def test_first_status_poll_is_a_full_snapshot():
    info = _table("station_information")
    status = _table("station_status")
    n_stations = len(set(info.column("station_id").to_pylist()))
    assert n_stations > 1500
    assert len(set(status.column("station_id").to_pylist())) >= 0.98 * n_stations


def test_poll_log_change_counts_match_status_rows():
    log = [r for r in _table("poll_log").to_pylist() if r["feed"] == "station_status" and r["ok"]]
    assert _table("station_status").num_rows >= sum(r["n_changed"] for r in log)
