"""Builds the dbt project on a tiny dataset with known answers. Run with `make test-dbt`."""

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rebalancer.ingest.lake import LakeWriter

pytestmark = pytest.mark.dbt

PROJECT = Path(__file__).parents[1] / "transform"
T0 = datetime(2026, 10, 20, 12, 0, 30, tzinfo=UTC)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def write_trips(raw: Path) -> None:
    def trip(ride, start, minutes, s_id, e_id):
        start_t = datetime.fromisoformat(start)
        return {
            "ride_id": ride,
            "rideable_type": "classic_bike",
            "started_at": start_t,
            "ended_at": start_t + timedelta(minutes=minutes),
            "start_station_name": "x",
            "start_station_id": s_id,
            "end_station_name": "y",
            "end_station_id": e_id,
            "start_lat": 40.75,
            "start_lng": -73.98,
            "end_lat": 40.76,
            "end_lng": -73.97,
            "member_casual": "member",
        }

    month_end = trip("DUP", "2026-09-30 23:55:00", 20, "1.01", "1.02")
    months = {
        "2026-09": [trip("A1", "2026-09-10 08:00:00", 12, "1.01", "1.02"), month_end],
        "2026-10": [trip("B1", "2026-10-05 09:00:00", 7, "1.02", "9.99"), month_end],
    }
    for month, rows in months.items():
        d = raw / "trips" / f"month={month}"
        d.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(rows), d / "trips.parquet")


def write_weather_and_holidays(raw: Path) -> None:
    hours = [T0.replace(minute=0, second=0) + timedelta(hours=i) for i in range(3)]
    for kind in ("observed", "forecast"):
        (raw / "weather").mkdir(parents=True, exist_ok=True)
        cols = {"time_utc": pa.array(hours, pa.timestamp("s", tz="UTC"))}
        for v in (
            "temperature_2m",
            "apparent_temperature",
            "precipitation",
            "rain",
            "snowfall",
            "wind_speed_10m",
        ):
            cols[v] = pa.array([10.0] * 3, pa.float32())
        cols["weather_code"] = pa.array([1] * 3, pa.int16())
        pq.write_table(pa.table(cols), raw / "weather" / f"{kind}.parquet")
    (raw / "holidays").mkdir()
    pq.write_table(
        pa.table({"date": pa.array([datetime(2026, 11, 26).date()]), "name": ["Thanksgiving"]}),
        raw / "holidays" / "holidays.parquet",
    )


def write_lake(lake: Path) -> None:
    w = LakeWriter(str(lake))

    def info(sid, short, cap, t):
        return {
            "station_id": sid,
            "short_name": short,
            "name": f"Station {sid}",
            "lat": 40.75,
            "lon": -73.98,
            "region_id": "71",
            "capacity": cap,
            "station_type": "classic",
            "has_kiosk": True,
            "feed_last_updated": int(t.timestamp()),
            "polled_at": ms(t),
        }

    t1, t2 = T0 + timedelta(minutes=5), T0 + timedelta(minutes=10)
    w.write(
        "station_information",
        [
            info("A", "1.01", 30, T0),
            info("B", "1.02", 20, T0),
            info("C", "1.03", 15, T0),
            info("A", "1.01", 30, t1),
            info("B", "1.02", 25, t1),
            info("C", "1.03", 15, t1),
            info("A", "1.01", 30, t2),
            info("B", "1.02", 25, t2),  # C removed
        ],
    )

    def status(sid, minute, bikes, docks, run_offset_s=0):
        t = T0 + timedelta(minutes=minute, seconds=run_offset_s)
        return {
            "station_id": sid,
            "legacy_id": "1",
            "num_bikes_available": bikes,
            "num_ebikes_available": 0,
            "num_bikes_disabled": 0,
            "num_docks_available": docks,
            "num_docks_disabled": 0,
            "is_installed": True,
            "is_renting": True,
            "is_returning": True,
            "last_reported": int(T0.timestamp()) + minute * 60,
            "feed_last_updated": int(t.timestamp()),
            "polled_at": ms(t),
        }

    rows = [
        status("A", 0, 5, 25),
        status("B", 0, 10, 10),
        status("C", 0, 3, 12),
        status("A", 1, 4, 26),
        status("A", 2, 9, 21),
        status("A", 3, 14, 16),  # +10 in two minutes: a van
        status("B", 4, 11, 14),
        status("B", 4, 11, 14, run_offset_s=20),  # same reading from an overlapping run
        status("A", 9, 13, 17),  # after the recorder gap at minutes 6-8
    ]
    w.write("station_status", rows)
    polls = [m for m in range(0, 11) if m not in (6, 7, 8)]
    w.write(
        "poll_log",
        [
            {
                "polled_at": ms(T0 + timedelta(minutes=m)),
                "feed": "station_status",
                "ok": True,
                "http_status": 200,
                "latency_ms": 50,
                "feed_last_updated": None,
                "n_stations": 3,
                "n_changed": 1,
                "error": None,
            }
            for m in polls
        ],
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("dbt")
    raw, lake, marts = root / "raw", root / "lake", root / "marts"
    write_trips(raw)
    write_weather_and_holidays(raw)
    write_lake(lake)
    marts.mkdir()
    env = {
        **os.environ,
        "RAW_ROOT": str(raw),
        "LAKE_ROOT": str(lake),
        "MARTS_ROOT": str(marts),
        "DBT_DUCKDB_PATH": str(root / "wh.duckdb"),
        "DBT_PROFILES_DIR": str(PROJECT),
    }
    result = subprocess.run(
        [
            "dbt",
            "build",
            "--project-dir",
            str(PROJECT),
            "--target-path",
            str(root / "target"),
            "--log-path",
            str(root / "logs"),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-4000:]
    return marts


def q(marts: Path, sql: str):
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con.execute(sql.replace("MARTS", marts.as_posix())).fetchall()


def test_month_end_ride_kept_once(built):
    rows = q(
        built,
        "SELECT ride_id, month FROM read_parquet('MARTS/fct_trips/*/*.parquet', "
        "hive_partitioning=true) ORDER BY 1",
    )
    assert rows == [("A1", "2026-09"), ("B1", "2026-10"), ("DUP", "2026-09")]


def test_trips_get_utc_times_and_station_ids(built):
    (row,) = q(
        built,
        "SELECT started_at_utc, start_station_id, end_station_id "
        "FROM read_parquet('MARTS/fct_trips/*/*.parquet') WHERE ride_id = 'B1'",
    )
    assert row[0] == datetime(2026, 10, 5, 13, 0, tzinfo=UTC)  # 9am EDT
    assert row[1] == "B" and row[2] is None  # 9.99 isn't a known station


def test_station_history_tracks_changes_and_removals(built):
    rows = q(
        built,
        "SELECT station_id, capacity, valid_from, valid_to, is_current "
        "FROM read_parquet('MARTS/dim_station.parquet') ORDER BY 1, 3",
    )
    b = [r for r in rows if r[0] == "B"]
    assert [r[1] for r in b] == [20, 25] and b[0][3] == b[1][2] and b[1][4]
    (c,) = [r for r in rows if r[0] == "C"]
    assert c[3] == T0 + timedelta(minutes=10) and not c[4]  # removed in the last snapshot


def test_minute_table_fills_gaps_and_flags_recorder_down(built):
    rows = q(
        built,
        "SELECT minute, num_bikes_available, recorder_up "
        "FROM read_parquet('MARTS/fct_station_minute.parquet') "
        "WHERE station_id = 'A' ORDER BY 1",
    )
    by_min = {int((r[0] - T0.replace(second=0)).total_seconds() // 60): r[1:] for r in rows}
    assert by_min[3] == (14, True)
    assert by_min[5] == (14, True)  # carried forward, no change
    assert by_min[7] == (14, False)  # recorder gap
    assert by_min[9] == (13, True)


def test_van_drop_off_detected_once(built):
    rows = q(
        built,
        "SELECT station_id, bikes_change, action, minutes "
        "FROM read_parquet('MARTS/fct_inferred_rebalancing.parquet')",
    )
    assert rows == [("A", 10, "drop_off", 2)]


def test_recorder_health_shows_the_gap(built):
    rows = q(
        built,
        "SELECT count(*) FILTER (WHERE NOT recorder_up), count(*) "
        "FROM read_parquet('MARTS/fct_recorder_minute.parquet')",
    )
    assert rows == [(3, 11)]


def test_report_lists_tables_and_van_visits(built):
    from rebalancer.warehouse import report

    text = report(built.parent)
    assert "| fct_trips | 3 rides" in text
    assert "1 likely van visits (1 drop-offs)" in text
    assert "**Station A**, drop_off +10" in text and "(gap)" not in text.split("drop_off")[1]
