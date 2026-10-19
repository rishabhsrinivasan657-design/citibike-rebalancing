import pyarrow.dataset as ds

from rebalancer.ingest.lake import LakeWriter

# 2026-10-19 13:59:30 UTC and 14:00:30 UTC, in ms
T1, T2 = 1792418370_000, 1792418430_000


def _status(station_id, polled_at, last_reported=1792418300):
    return {
        "station_id": station_id,
        "legacy_id": "1",
        "num_bikes_available": 3,
        "num_ebikes_available": 1,
        "num_bikes_disabled": 0,
        "num_docks_available": 7,
        "num_docks_disabled": 0,
        "is_installed": True,
        "is_renting": True,
        "is_returning": True,
        "last_reported": last_reported,
        "feed_last_updated": 1792418360,
        "polled_at": polled_at,
    }


def test_writes_hour_partitions_and_converts_times(tmp_path):
    writer = LakeWriter(str(tmp_path))
    paths = writer.write("station_status", [_status("a", T1), _status("b", T2), _status("c", T2)])
    assert len(paths) == 2
    assert any("date=2026-10-19/hour=13" in p for p in paths)
    assert any("date=2026-10-19/hour=14" in p for p in paths)
    assert not list(tmp_path.rglob("*.tmp"))

    table = ds.dataset(
        tmp_path / "station_status", format="parquet", partitioning="hive"
    ).to_table()
    assert table.num_rows == 3
    row = table.filter(ds.field("station_id") == "a").to_pylist()[0]
    assert row["last_reported"].timestamp() == 1792418300
    assert row["polled_at"].timestamp() == T1 / 1000


def test_nullable_poll_log_fields(tmp_path):
    rec = {
        "polled_at": T1,
        "feed": "station_status",
        "ok": False,
        "http_status": None,
        "latency_ms": 20000,
        "feed_last_updated": None,
        "n_stations": None,
        "n_changed": None,
        "error": "ConnectTimeout",
    }
    LakeWriter(str(tmp_path)).write("poll_log", [rec])
    table = ds.dataset(tmp_path / "poll_log", format="parquet", partitioning="hive").to_table()
    assert table.to_pylist()[0]["error"] == "ConnectTimeout"
