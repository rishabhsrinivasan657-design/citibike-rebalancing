"""Use the demand model: expected departures and arrivals per station per hour.

Weather comes from Open-Meteo: the live forecast for today and tomorrow, and the
historical-forecast archive (what was forecast at the time) for past days.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import holidays as holidays_lib
import httpx
import joblib
import numpy as np
import pyarrow.parquet as pq

from rebalancer.backfill.weather import CENTRAL_PARK
from rebalancer.forecast.train import FEATURES, NYC_LAT, approved

LIVE_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

Weather = dict[datetime, tuple[float, float, float, float]]  # UTC hour -> temp, precip, snow, wind


class ModelNotApproved(Exception):
    """The saved model didn't beat the averages on its test month."""


def daylight_hours(day: date) -> float:
    """Same formula as train.DAYLIGHT_SQL."""
    import math

    decl = math.radians(23.44 * math.sin(2 * math.pi * (284 + day.timetuple().tm_yday) / 365))
    return 24 / math.pi * math.acos(-math.tan(math.radians(NYC_LAT)) * math.tan(decl))


def fetch_weather(client: httpx.Client, day: date, retries: int = 4) -> Weather:
    """Hourly forecast weather around a New York calendar day, keyed by UTC hour.
    Retries with backoff: one Open-Meteo timeout failed the weekly verify (Oct 2026)."""
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
    for attempt in range(retries):
        try:
            resp = client.get(LIVE_URL if recent else ARCHIVE_URL, params=params)
            resp.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt + 1 == retries:
                raise
            time.sleep(2**attempt)
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
    dow_profile: dict[tuple[str, int, int], tuple[float, float]] = field(default_factory=dict)
    recent_profile: dict[tuple[str, str, int], tuple[float, float]] = field(default_factory=dict)

    @property
    def features(self) -> list[str]:
        return self.bundle.get("features", FEATURES)

    @classmethod
    def load(
        cls,
        model_path: Path,
        profile_path: Path,
        dow_path: Path | None = None,
        recent_path: Path | None = None,
    ) -> DemandModel:
        bundle = joblib.load(model_path)
        if not bundle.get("approved") or not approved(bundle.get("metrics", {})):
            raise ModelNotApproved("the saved demand model didn't beat the averages")

        def table(path, keys, cols):
            if path is None:
                return {}
            return {
                tuple(r[k] for k in keys): tuple(r[c] for c in cols)
                for r in pq.read_table(path).to_pylist()
            }

        return cls(
            bundle,
            table(
                profile_path, ("short_name", "day_type", "hour"), ("avg_departures", "avg_arrivals")
            ),
            table(
                dow_path, ("short_name", "dow", "hour"), ("dow_avg_departures", "dow_avg_arrivals")
            ),
            table(
                recent_path,
                ("short_name", "day_type", "hour"),
                ("recent_avg_departures", "recent_avg_arrivals"),
            ),
        )

    def hourly(
        self,
        short_names: list[str],
        start_local: datetime,
        hours: int,
        weather: Weather,
        capacity: dict[str, float] | None = None,
    ) -> dict[str, dict[int, tuple[float, float]]]:
        """{short_name: {local hour: (departures, arrivals)}} for each hour from start."""
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        capacity = capacity or {}
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
                dow_d, dow_a = self.dow_profile.get((sn, dow, t.hour), (0.0, 0.0))
                rec_d, rec_a = self.recent_profile.get((sn, day_type, t.hour), (0.0, 0.0))
                values = {
                    "avg_departures": avg_d,
                    "avg_arrivals": avg_a,
                    "hour": t.hour,
                    "dow": dow,
                    "is_holiday": int(hol),
                    "daylight_hours": daylight_hours(t.date()),
                    "temp_c": temp,
                    "precip_mm": precip,
                    "snow_cm": snow,
                    "wind_kmh": wind,
                    "dow_avg_departures": dow_d,
                    "dow_avg_arrivals": dow_a,
                    "recent_avg_departures": rec_d,
                    "recent_avg_arrivals": rec_a,
                    "capacity": capacity.get(sn, np.nan),
                }
                rows.append([values[f] for f in self.features])
                keys.append((sn, t.hour))
        x = np.array(rows, dtype=float)
        # Safety net: never predict more than 1.5x the busiest station-hour ever seen.
        cap = 1.5 * self.bundle["max_target"]
        dep = np.clip(self.bundle["departures"].predict(x), 0, cap)
        arr = np.clip(self.bundle["arrivals"].predict(x), 0, cap)
        out: dict[str, dict[int, tuple[float, float]]] = {}
        for (sn, hour), d, a in zip(keys, dep, arr, strict=True):
            out.setdefault(sn, {})[hour] = (float(d), float(a))
        return out
