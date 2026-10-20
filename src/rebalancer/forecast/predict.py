"""Use the demand model: expected departures and arrivals per station per hour.

Weather comes from Open-Meteo: the live forecast for today and tomorrow, and the
historical-forecast archive (what was forecast at the time) for past days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import holidays as holidays_lib
import httpx
import joblib
import numpy as np
import pyarrow.parquet as pq

from rebalancer.backfill.weather import CENTRAL_PARK
from rebalancer.forecast.train import FEATURES

LIVE_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

Weather = dict[datetime, tuple[float, float, float, float]]  # UTC hour -> temp, precip, snow, wind


def fetch_weather(client: httpx.Client, day: date) -> Weather:
    """Hourly forecast weather around a New York calendar day, keyed by UTC hour."""
    lat, lon = CENTRAL_PARK
    recent = day >= date.today() - timedelta(days=1)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,snowfall,wind_speed_10m",
        "timezone": "UTC",
    }
    if recent:
        params.update({"past_days": 2, "forecast_days": 3})
    else:
        params.update(
            {
                "start_date": (day - timedelta(days=1)).isoformat(),
                "end_date": (day + timedelta(days=1)).isoformat(),
            }
        )
    resp = client.get(LIVE_URL if recent else ARCHIVE_URL, params=params)
    resp.raise_for_status()
    h = resp.json()["hourly"]
    return {
        datetime.fromisoformat(t).replace(tzinfo=UTC): (
            h["temperature_2m"][i],
            h["precipitation"][i],
            h["snowfall"][i],
            h["wind_speed_10m"][i],
        )
        for i, t in enumerate(h["time"])
    }


@dataclass
class DemandModel:
    bundle: dict
    profile: dict[tuple[str, str, int], tuple[float, float]]

    @classmethod
    def load(cls, model_path: Path, profile_path: Path) -> DemandModel:
        rows = pq.read_table(profile_path).to_pylist()
        profile = {
            (r["short_name"], r["day_type"], r["hour"]): (r["avg_departures"], r["avg_arrivals"])
            for r in rows
        }
        return cls(joblib.load(model_path), profile)

    def hourly(
        self, short_names: list[str], start_local: datetime, hours: int, weather: Weather
    ) -> dict[str, dict[int, tuple[float, float]]]:
        """{short_name: {local hour: (departures, arrivals)}} for each hour from start."""
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        first = start_local.replace(minute=0, second=0, microsecond=0)
        rows, keys = [], []
        for k in range(hours + 1):
            t = first + timedelta(hours=k)
            hol = t.date() in holidays_lib.country_holidays("US", subdiv="NY", years=t.year)
            day_type = "weekend" if t.weekday() >= 5 or hol else "weekday"
            utc_hour = t.replace(tzinfo=ny).astimezone(UTC)
            temp, precip, snow, wind = weather.get(utc_hour, (np.nan,) * 4)
            dow = (t.weekday() + 1) % 7  # DuckDB dayofweek: Sunday = 0
            for sn in short_names:
                avg_d, avg_a = self.profile.get((sn, day_type, t.hour), (0.0, 0.0))
                values = {
                    "avg_departures": avg_d,
                    "avg_arrivals": avg_a,
                    "hour": t.hour,
                    "dow": dow,
                    "is_holiday": int(hol),
                    "month": t.month,
                    "temp_c": temp,
                    "precip_mm": precip,
                    "snow_cm": snow,
                    "wind_kmh": wind,
                }
                rows.append([values[f] for f in FEATURES])
                keys.append((sn, t.hour))
        x = np.array(rows, dtype=float)
        dep = self.bundle["departures"].predict(x)
        arr = self.bundle["arrivals"].predict(x)
        out: dict[str, dict[int, tuple[float, float]]] = {}
        for (sn, hour), d, a in zip(keys, dep, arr, strict=True):
            out.setdefault(sn, {})[hour] = (float(d), float(a))
        return out
