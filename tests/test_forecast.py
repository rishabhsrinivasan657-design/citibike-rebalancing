from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rebalancer.backfill import db, holidays_calendar
from rebalancer.forecast import train
from rebalancer.forecast.predict import DemandModel

NY = ZoneInfo("America/New_York")


def rainy(d: date) -> bool:
    return d.toordinal() % 3 == 0


def days():
    """Weekdays in Jun-Jul 2026 (training) and September 2026 (test)."""
    for start, end in (
        (date(2026, 6, 1), date(2026, 7, 31)),
        (date(2026, 9, 1), date(2026, 9, 30)),
    ):
        d = start
        while d <= end:
            if d.weekday() < 5:
                yield d
            d += timedelta(days=1)


@pytest.fixture(scope="module")
def raw(tmp_path_factory):
    raw = tmp_path_factory.mktemp("raw")
    rides, n = [], 0
    for d in days():
        for k in range(2 if rainy(d) else 10):  # riders avoid the rain at 8am
            t = datetime(d.year, d.month, d.day, 8, 5 * k)
            n += 1
            rides.append(
                {
                    "ride_id": f"r{n}",
                    "started_at": t,
                    "ended_at": t + timedelta(minutes=9),
                    "start_station_id": "1.01",
                    "end_station_id": "1.02",
                }
            )
    for month in sorted({r["started_at"].strftime("%Y-%m") for r in rides}):
        out = raw / "trips" / f"month={month}"
        out.mkdir(parents=True)
        rows = [r for r in rides if r["started_at"].strftime("%Y-%m") == month]
        pq.write_table(pa.Table.from_pylist(rows), out / "trips.parquet")

    hours, precip = [], []
    t = datetime(2026, 5, 31, tzinfo=UTC)
    while t < datetime(2026, 10, 2, tzinfo=UTC):
        hours.append(t)
        precip.append(3.0 if rainy(t.astimezone(NY).date()) else 0.0)
        t += timedelta(hours=1)
    (raw / "weather").mkdir()
    pq.write_table(
        pa.table(
            {
                "time_utc": pa.array(hours, pa.timestamp("s", tz="UTC")),
                "temperature_2m": [20.0] * len(hours),
                "precipitation": precip,
                "snowfall": [0.0] * len(hours),
                "wind_speed_10m": [10.0] * len(hours),
            }
        ),
        raw / "weather" / "forecast.parquet",
    )
    holidays_calendar.backfill(raw, [2026])
    return raw


@pytest.fixture(scope="module")
def con(raw):
    c = db.connect()
    train.build_table(c, raw)
    return c


def test_table_has_every_hour_including_zeros(con):
    n, zeros, first, last = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE departures = 0 AND arrivals = 0), "
        "min(hour_local), max(hour_local) FROM station_hour WHERE short_name = '1.01'"
    ).fetchone()
    hours = int((last - first).total_seconds() // 3600) + 1
    assert n == hours and zeros > n * 0.9


def test_rain_comes_from_the_forecast_for_that_local_hour(con):
    d = next(x for x in days() if rainy(x))
    (precip,) = con.execute(
        "SELECT precip_mm FROM station_hour WHERE short_name = '1.01' AND hour_local = ?",
        [datetime(d.year, d.month, d.day, 8)],
    ).fetchone()
    assert precip == 3.0


def test_model_beats_averages_when_weather_matters(con):
    results, report = train.evaluate(con, sample=10_000_000)
    dep = results["departures"]
    assert dep["model_rush"]["mae"] < dep["averages_rush"]["mae"]
    assert "September 2026 test" in report


def test_saved_model_predicts_less_riding_in_the_rain(con, tmp_path):
    train.train_production(con, tmp_path, 10_000_000, metrics={})
    model = DemandModel.load(tmp_path / "demand.joblib", tmp_path / "profile_features.parquet")
    start = datetime(2026, 9, 15, 8, 0)  # a Tuesday
    utc8 = start.replace(tzinfo=NY).astimezone(UTC)
    dry = model.hourly(["1.01"], start, 0, {utc8: (20.0, 0.0, 0.0, 10.0)})["1.01"][8][0]
    wet = model.hourly(["1.01"], start, 0, {utc8: (20.0, 3.0, 0.0, 10.0)})["1.01"][8][0]
    assert dry > 6 and wet < 4
