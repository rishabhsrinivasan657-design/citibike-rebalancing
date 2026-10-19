"""Hourly Central Park weather from Open-Meteo: what happened, and what was forecast.

<raw>/weather/observed.parquet   (ERA5-based archive)
<raw>/weather/forecast.parquet   (archived forecasts, what we'd have known live)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

CENTRAL_PARK = (40.7829, -73.9654)
VARIABLES = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "snowfall",
    "wind_speed_10m",
    "weather_code",
]
ENDPOINTS = {
    "observed": "https://archive-api.open-meteo.com/v1/archive",
    "forecast": "https://historical-forecast-api.open-meteo.com/v1/forecast",
}


def fetch(client: httpx.Client, kind: str, start: str, end: str) -> dict:
    lat, lon = CENTRAL_PARK
    resp = client.get(
        ENDPOINTS[kind],
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(VARIABLES),
            "timezone": "UTC",
        },
    )
    resp.raise_for_status()
    return resp.json()


def to_table(payload: dict) -> pa.Table:
    hourly = payload["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]
    columns = {"time_utc": pa.array(times, type=pa.timestamp("s", tz="UTC"))}
    for var in VARIABLES:
        typ = pa.int16() if var == "weather_code" else pa.float32()
        columns[var] = pa.array(hourly[var], type=typ)
    return pa.table(columns)


def missing_hours(table: pa.Table) -> list[datetime]:
    """Hours absent from the series, or present with any null value."""
    times = table.column("time_utc").to_pylist()
    if not times:
        return []
    have = {t for i, t in enumerate(times) if all(table.column(v)[i].is_valid for v in VARIABLES)}
    out, t = [], min(times)
    while t <= max(times):
        if t not in have:
            out.append(t)
        t += timedelta(hours=1)
    return out


def backfill(client: httpx.Client, raw: Path, start: str, end: str) -> dict[str, int]:
    out_dir = raw / "weather"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = {}
    for kind in ENDPOINTS:
        table = to_table(fetch(client, kind, start, end))
        pq.write_table(table, out_dir / f"{kind}.parquet", compression="zstd")
        rows[kind] = table.num_rows
    return rows
