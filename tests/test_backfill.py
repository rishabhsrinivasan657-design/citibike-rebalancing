import io
import zipfile
from datetime import UTC, datetime

import duckdb
import httpx
import pyarrow.parquet as pq
import pytest
import respx

from rebalancer.backfill import checks, holidays_calendar, trips, weather

HEADER = (
    "ride_id,rideable_type,started_at,ended_at,start_station_name,start_station_id,"
    "end_station_name,end_station_id,start_lat,start_lng,end_lat,end_lng,member_casual\n"
)


def _row(ride, start_id="7293.10", started="2026-04-06 18:55:38.804", end_id="7456.03"):
    return (
        f"{ride},classic_bike,{started},2026-04-06 19:04:03.989,"
        f'"E 88 St, Park Ave",{start_id},E 106 St & 1 Ave,{end_id},'
        "40.78,-73.95,40.79,-73.93,member\n"
    )


def _zip_bytes(rows_part1, rows_part2) -> bytes:
    """A month zip with one top-level CSV and one CSV inside a nested zip."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("202604-citibike-tripdata_2.csv", HEADER + "".join(rows_part2))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("202604-citibike-tripdata_1.csv", HEADER + "".join(rows_part1))
        z.writestr("nested/inner.zip", inner.getvalue())
        z.writestr("__MACOSX/._junk.csv", "junk")
    return outer.getvalue()


S3_LISTING = """<ListBucketResult>
<Contents><Key>202604-citibike-tripdata.zip</Key><ETag>&quot;abc123&quot;</ETag><Size>999</Size></Contents>
<Contents><Key>202605-citibike-tripdata.csv.zip</Key><ETag>&quot;def&quot;</ETag><Size>5</Size></Contents>
<Contents><Key>JC-202604-citibike-tripdata.csv.zip</Key><ETag>&quot;x&quot;</ETag><Size>1</Size></Contents>
</ListBucketResult>"""


def test_months_between_crosses_year():
    assert trips.months_between("2025-11", "2026-02") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


@respx.mock
def test_list_sources_parses_nyc_files_only():
    respx.get(trips.BUCKET_URL).mock(return_value=httpx.Response(200, text=S3_LISTING))
    with httpx.Client() as c:
        sources = trips.list_sources(c)
    assert set(sources) == {"2026-04", "2026-05"}
    assert sources["2026-04"].etag == "abc123" and sources["2026-04"].size == 999
    assert sources["2026-05"].url.endswith("202605-citibike-tripdata.csv.zip")


def test_count_data_rows(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text(HEADER + _row("A") + _row("B"))
    b = tmp_path / "b.csv"
    b.write_text(HEADER + _row("A") + _row("B").rstrip("\n"))
    assert trips.count_data_rows(a) == 2 and trips.count_data_rows(b) == 2


@respx.mock
def test_backfill_month_end_to_end(tmp_path):
    payload = _zip_bytes([_row("A"), _row("B", start_id="5414.07")], [_row("C", start_id="")])
    source = trips.SourceFile(
        "202604-citibike-tripdata.zip",
        trips.BUCKET_URL + "202604-citibike-tripdata.zip",
        "abc123",
        1,
    )
    respx.get(source.url).mock(return_value=httpx.Response(200, content=payload))

    with httpx.Client() as c:
        man = trips.backfill_month(c, tmp_path, "2026-04", source)

    assert man.csv_rows == man.parquet_rows == 3
    assert len(man.csv_files) == 2  # nested zip found, __MACOSX ignored
    assert trips.is_current(tmp_path, "2026-04", source)
    assert not trips.is_current(tmp_path, "2026-04", trips.SourceFile("k", "u", "new-etag", 1))

    table = pq.read_table(tmp_path / "trips" / "month=2026-04" / "trips.parquet")
    ids = table.column("start_station_id").to_pylist()
    assert "7293.10" in ids  # not trimmed to 7293.1
    assert None in ids  # empty start station becomes null
    assert table.schema.field("started_at").type.unit == "us"
    assert table.column("start_station_name").to_pylist()[0] == "E 88 St, Park Ave"


@respx.mock
def test_trip_checks_catch_duplicates_and_unknown_stations(tmp_path):
    payload = _zip_bytes([_row("A"), _row("A"), _row("B", start_id="9999.99")], [])
    source = trips.SourceFile("k.zip", trips.BUCKET_URL + "k.zip", "e", 1)
    respx.get(source.url).mock(return_value=httpx.Response(200, content=payload))
    with httpx.Client() as c:
        trips.backfill_month(c, tmp_path, "2026-04", source)

    results = {c.name: c for c in checks.trip_checks(tmp_path, ["2026-04", "2026-05"], {"7293.10"})}
    assert not results["Every month downloaded"].passed
    assert results["Trip counts match Citi Bike's files"].passed
    assert not results["No trip appears twice in the same month"].passed
    match = results["At least 97% of trips start at a known station"]
    assert not match.passed and "66.67%" in match.detail
    assert results["Station IDs kept exactly as written (stored as text)"].passed
    assert results["Trips are filed under the month they started"].passed


def test_convert_fails_loudly_on_bad_rows(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + _row("A", started="not-a-time"))
    try:
        trips.convert([bad], tmp_path / "out.parquet")
    except duckdb.Error:
        pass
    else:
        raise AssertionError("expected a parse error")


def _weather_payload(times, temp):
    hourly = {"time": times}
    for v in weather.VARIABLES:
        hourly[v] = [1 for _ in times]
    hourly["temperature_2m"] = temp
    return {"hourly": hourly}


def test_weather_missing_hours_detects_gaps_and_nulls():
    payload = _weather_payload(
        ["2026-01-01T00:00", "2026-01-01T01:00", "2026-01-01T03:00"], [1.0, None, 2.0]
    )
    table = weather.to_table(payload)
    assert weather.missing_hours(table) == [
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 2, tzinfo=UTC),
    ]


def test_weather_complete_series_has_no_gaps():
    payload = _weather_payload(["2026-01-01T00:00", "2026-01-01T01:00"], [1.0, 2.0])
    assert weather.missing_hours(weather.to_table(payload)) == []


def test_holidays(tmp_path):
    holidays_calendar.backfill(tmp_path, [2025, 2026])
    (result,) = checks.holiday_checks(tmp_path, [2025, 2026])
    assert result.passed, result.detail


@respx.mock
def test_month_end_ride_in_two_files_is_not_a_failure(tmp_path):
    late = _row("X", started="2026-04-30 23:59:34.554")
    for month, key in (("2026-04", "a.zip"), ("2026-05", "b.zip")):
        src = trips.SourceFile(key, trips.BUCKET_URL + key, "e", 1)
        respx.get(src.url).mock(return_value=httpx.Response(200, content=_zip_bytes([late], [])))
        with httpx.Client() as c:
            trips.backfill_month(c, tmp_path, month, src)
    results = {c.name: c for c in checks.trip_checks(tmp_path, ["2026-04", "2026-05"], set())}
    dup = results["No trip appears twice in the same month"]
    assert dup.passed and "1 rides listed in two months" in dup.detail


def test_duckdb_connections_are_memory_capped(monkeypatch):
    from rebalancer.backfill import db

    monkeypatch.setenv("DUCKDB_MEMORY_LIMIT", "512MB")
    con = db.connect()
    limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert limit in ("512.0 MiB", "488.2 MiB", "512.0 MB", "488.2 MB"), limit


class FakeHubApi:
    def __init__(self):
        self.uploads = []

    def whoami(self):
        return {"name": "alice", "auth": {"accessToken": {"role": "write"}}, "orgs": []}

    def create_repo(self, repo_id, **kw):
        self.repo = (repo_id, kw)

    def upload_folder(self, **kw):
        self.uploads.append(("folder", kw["path_in_repo"]))

    def upload_file(self, **kw):
        self.uploads.append(("file", kw["path_in_repo"]))


def test_hub_sync_paths(tmp_path):
    from rebalancer.backfill.hub import HubSync

    raw = tmp_path / "raw"
    (raw / "trips" / "month=2026-04").mkdir(parents=True)
    (raw / "_checks.md").write_text("x")
    api = FakeHubApi()
    sync = HubSync("citibike-history", "tok", raw, api=api)
    assert sync.repo_id == "alice/citibike-history" and api.repo[1]["private"] is True
    sync.push(raw / "trips" / "month=2026-04")
    sync.push(raw / "_checks.md")
    assert api.uploads == [("folder", "raw/trips/month=2026-04"), ("file", "raw/_checks.md")]


def test_hub_sync_requires_raw_folder_name(tmp_path):
    import pytest

    from rebalancer.backfill.hub import HubSync

    with pytest.raises(ValueError):
        HubSync("x", "tok", tmp_path / "data", api=FakeHubApi())


@respx.mock
def test_cmd_trips_pushes_each_month(tmp_path):
    from rebalancer.backfill import cli

    respx.get(trips.BUCKET_URL).mock(return_value=httpx.Response(200, text=S3_LISTING))
    respx.get(trips.BUCKET_URL + "202604-citibike-tripdata.zip").mock(
        return_value=httpx.Response(200, content=_zip_bytes([_row("A")], []))
    )
    pushed = []
    cli.cmd_trips(tmp_path, "2026-04", "2026-04", force=False, push=pushed.append)
    cli.cmd_trips(tmp_path, "2026-04", "2026-04", force=False, push=pushed.append)  # skipped
    assert pushed == [trips.month_dir(tmp_path, "2026-04")]


@respx.mock
def test_profiles_average_over_all_days_and_add_up(tmp_path):
    from rebalancer.backfill import profiles

    rows = [
        _row("A", started="2026-04-06 08:10:00.000"),  # Monday
        _row("B", started="2026-04-06 08:40:00.000"),
        _row("C", started="2026-04-11 08:05:00.000"),  # Saturday
    ]
    src = trips.SourceFile("a.zip", trips.BUCKET_URL + "a.zip", "e", 1)
    respx.get(src.url).mock(return_value=httpx.Response(200, content=_zip_bytes(rows, [])))
    with httpx.Client() as c:
        trips.backfill_month(c, tmp_path, "2026-04", src)
    holidays_calendar.backfill(tmp_path, [2026])

    assert profiles.build(tmp_path, "2026-04", "2026-04") > 0
    table = pq.read_table(tmp_path / "profiles" / "station_hour.parquet").to_pylist()
    wk = next(
        r
        for r in table
        if r["station_id"] == "7293.10" and r["day_type"] == "weekday" and r["hour"] == 8
    )
    assert wk["days"] == 22  # April 2026 has 22 weekdays and no NY holidays
    assert wk["departures"] == pytest.approx(2 / 22)
    (check,) = checks.profile_checks(tmp_path)
    assert check.passed, check.detail
