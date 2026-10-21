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
    (raw / "profiles").mkdir()
    pq.write_table(
        pa.Table.from_pylist(
            [
                # quiet station: ~6 riders an hour each way at 8am on weekdays
                {
                    "station_id": "1.01",
                    "day_type": "weekday",
                    "hour": 8,
                    "departures": 6.0,
                    "arrivals": 6.0,
                    "days": 22,
                },
                # very busy station: ~5 returns a minute at 8am
                {
                    "station_id": "1.02",
                    "day_type": "weekday",
                    "hour": 8,
                    "departures": 30.0,
                    "arrivals": 300.0,
                    "days": 22,
                },
                # Jersey City looks unused: its trips are in files we don't download
                {
                    "station_id": "2.01",
                    "day_type": "weekday",
                    "hour": 8,
                    "departures": 0.1,
                    "arrivals": 0.1,
                    "days": 22,
                },
            ]
        ),
        raw / "profiles" / "station_hour.parquet",
    )
    (raw / "holidays").mkdir()
    pq.write_table(
        pa.table({"date": pa.array([datetime(2026, 11, 26).date()]), "name": ["Thanksgiving"]}),
        raw / "holidays" / "holidays.parquet",
    )


def write_lake(lake: Path) -> None:
    w = LakeWriter(str(lake))

    def info(sid, short, cap, t, region="71"):
        return {
            "station_id": sid,
            "short_name": short,
            "name": f"Station {sid}",
            "lat": 40.75,
            "lon": -73.98,
            "region_id": region,
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
            info("J", "2.01", 30, T0, region="70"),  # Jersey City
            info("J", "2.01", 30, t1, region="70"),
            info("J", "2.01", 30, t2, region="70"),
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
        status("A", 3, 14, 16),  # quiet station +10 in two minutes: a van...
        status("A", 4, 13, 17),  # ...one rider takes a bike mid-visit...
        status("A", 5, 21, 9),  # ...then the van unloads 8 more: still the same visit
        status("B", 2, 13, 7),
        status("B", 3, 16, 4),  # busy station +6 in two minutes: normal for riders
        status("B", 3, 16, 4, run_offset_s=20),  # same reading from an overlapping run
        status("J", 1, 5, 25),
        status("J", 3, 15, 15),  # Jersey City +10: skipped, no reliable profile there
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
    assert by_min[5] == (21, True)
    assert by_min[7] == (21, False)  # recorder gap: carried forward
    assert by_min[9] == (13, True)


def test_van_visits_quiet_station_only_and_split_visit_merged(built):
    rows = q(
        built,
        "SELECT station_id, bikes_change, action, minutes, runs_merged, expected_riders, "
        "p_riders < 1e-6 FROM read_parquet('MARTS/fct_inferred_rebalancing.parquet')",
    )
    # A: quiet station, +10 then (after one rider) +8 more: one van visit of +18.
    # B: riders normally return ~10 bikes in 2 minutes there, so +6 isn't flagged.
    # J: Jersey City is skipped (no reliable profile), despite its +10.
    assert rows == [("A", 18, "drop_off", 3, 2, 0.3, True)]


def test_busy_and_jersey_city_cases_are_really_in_the_data(built):
    # Guards against the fixture silently missing the cases the test above relies on.
    rows = q(
        built,
        "SELECT station_id, max(num_bikes_available) - min(num_bikes_available) "
        "FROM read_parquet('MARTS/fct_station_minute.parquet') "
        "WHERE station_id IN ('B', 'J') GROUP BY 1 ORDER BY 1",
    )
    assert rows == [("B", 6), ("J", 10)]


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
    assert "**Station A**: +18 bikes in 3 min (riders usually ~0.3)" in text


def test_every_model_and_mart_column_is_documented(built):
    """The OKF bundle (Step 8) is generated from these descriptions."""
    import json

    import pyarrow.dataset as pads

    manifest = json.loads((built.parent / "target" / "manifest.json").read_text())
    models = {n["name"]: n for n in manifest["nodes"].values() if n["resource_type"] == "model"}
    undocumented = sorted(name for name, n in models.items() if not n["description"].strip())
    assert not undocumented, f"models without a description: {undocumented}"

    missing = []
    for path in sorted(built.iterdir()):
        name = path.name.removesuffix(".parquet")
        actual = pads.dataset(path, format="parquet", partitioning="hive").schema.names
        documented = {c for c, v in models[name]["columns"].items() if v["description"].strip()}
        missing += [f"{name}.{c}" for c in actual if c not in documented]
    assert not missing, f"mart columns without a description: {missing}"


@pytest.fixture(scope="module")
def bundle(built, tmp_path_factory):
    """Generate the OKF bundle from the test warehouse, like the transform workflow does."""
    from rebalancer.okf.generate import generate

    root = built.parent
    env = {
        **os.environ,
        "RAW_ROOT": str(root / "raw"),
        "LAKE_ROOT": str(root / "lake"),
        "MARTS_ROOT": str(built),
        "DBT_DUCKDB_PATH": str(root / "wh.duckdb"),
        "DBT_PROFILES_DIR": str(PROJECT),
    }
    common = [
        "--project-dir",
        str(PROJECT),
        "--target-path",
        str(root / "target"),
        "--log-path",
        str(root / "logs"),
    ]
    import shutil

    target = root / "target"
    shutil.copy(target / "run_results.json", target / "build_results.json")  # as the workflow does
    subprocess.run(["dbt", "docs", "generate", *common], env=env, capture_output=True, check=True)
    # The fixture's live data is from 2026-10-20, so freshness fails: that's the point.
    subprocess.run(["dbt", "source", "freshness", *common], env=env, capture_output=True)
    out = tmp_path_factory.mktemp("okf") / "bundle"
    counts = generate(root / "target", Path(__file__).parents[1] / "okf" / "static", out)
    return out, counts


def test_bundle_is_valid_with_no_warnings(bundle):
    from rebalancer.okf.validate import validate

    out, counts = bundle
    rep = validate(out)
    assert rep.ok and rep.warnings == [], rep.errors + rep.warnings
    assert counts["tables"] >= 14 and counts["sources"] == 8


def test_table_concept_has_schema_joins_lineage_and_trust(bundle):
    from rebalancer.okf.generate import read_frontmatter

    out, _ = bundle
    fm = read_frontmatter(out / "tables" / "fct_station_minute.md")
    text = (out / "tables" / "fct_station_minute.md").read_text()
    assert fm["type"] == "Table" and "marts" in fm["tags"]
    assert fm["resource"].endswith("/marts/fct_station_minute.parquet")
    assert {s["resource"] for s in fm["sources"]} >= {
        "/tables/stg_station_status.md",
        "/tables/int_recorder_minute.md",
    }
    assert fm["verified"] == [{"by": "process:dbt-tests", "at": fm["generated"]["at"]}]
    assert "| `num_bikes_available` | INTEGER | Bikes ready to rent" in text
    assert "Joins [dim_station](/tables/dim_station.md) using `station_id`" in text
    assert "# Caveats" in text and "carried forward" in text


def test_live_tables_go_stale_within_hours_history_tables_within_weeks(bundle):
    from rebalancer.okf.generate import parse_time, read_frontmatter

    out, _ = bundle

    def stale(name):
        fm = read_frontmatter(out / "tables" / f"{name}.md")
        return parse_time(str(fm["stale_after"])) - parse_time(str(fm["generated"]["at"]))

    assert stale("fct_station_minute").total_seconds() == 7 * 3600
    assert stale("fct_trips").days == 35


def test_stale_source_is_marked_stale_and_not_verified(bundle):
    from rebalancer.okf.generate import parse_time, read_frontmatter

    out, _ = bundle
    fm = read_frontmatter(out / "sources" / "live__poll_log.md")
    assert parse_time(str(fm["stale_after"])) < datetime.now(UTC)  # fixture data is old
    assert not fm.get("verified")
    assert "status **error**" in (out / "sources" / "live__poll_log.md").read_text()


def test_glossary_and_indexes_are_included(bundle):
    out, _ = bundle
    root_index = (out / "index.md").read_text()
    assert root_index.startswith('---\nokf_version: "0.2"\n---')
    assert "[glossary](glossary/index.md)" in root_index
    assert (out / "glossary" / "riders-affected.md").exists()
    tables_index = (out / "tables" / "index.md").read_text().splitlines()
    first_entries = [line for line in tables_index if line.startswith("* ")][:5]
    assert all(any(m in e for m in ("dim_station", "fct_")) for e in first_entries)  # marts first
    assert "## " in (out / "log.md").read_text()
