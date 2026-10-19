"""GBFS feed models, discovery, and fetching.

Only the fields we use are modelled; unknown fields are ignored so that additive
changes on Lyft's side don't break ingestion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)


class StationStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    station_id: str
    legacy_id: str | None = None
    num_bikes_available: int
    num_ebikes_available: int = 0
    num_bikes_disabled: int = 0
    num_docks_available: int
    num_docks_disabled: int = 0
    is_installed: bool
    is_renting: bool
    is_returning: bool
    last_reported: int


class StationInformation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    station_id: str
    short_name: str | None = None
    name: str
    lat: float
    lon: float
    region_id: str | None = None
    capacity: int
    station_type: str | None = None
    has_kiosk: bool | None = None


@dataclass(frozen=True)
class FeedSnapshot[T]:
    last_updated: int
    ttl: int
    stations: list[T]
    content_hash: str


@dataclass
class FetchResult:
    """Outcome of one HTTP fetch, kept even on failure so it can be logged to poll_log."""

    ok: bool
    http_status: int | None
    latency_ms: int
    payload: dict | None = None
    error: str | None = None


def discover_feeds(client: httpx.Client, discovery_url: str, language: str) -> dict[str, str]:
    """Return {feed_name: url} from the GBFS auto-discovery file."""
    resp = client.get(discovery_url)
    resp.raise_for_status()
    feeds = resp.json()["data"][language]["feeds"]
    return {f["name"]: f["url"] for f in feeds}


def fetch_json(client: httpx.Client, url: str, retries: int) -> FetchResult:
    """GET a JSON document with exponential backoff. Never raises."""
    status: int | None = None
    error: str | None = None
    start = time.monotonic()
    for attempt in range(retries):
        try:
            resp = client.get(url)
            status = resp.status_code
            resp.raise_for_status()
            return FetchResult(True, status, _ms_since(start), payload=resp.json())
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("fetch failed (attempt %d/%d) %s: %s", attempt + 1, retries, url, error)
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    return FetchResult(False, status, _ms_since(start), error=error)


def parse_feed[T: BaseModel](payload: dict, model: type[T]) -> FeedSnapshot[T]:
    """Validate a GBFS station feed. Raises pydantic.ValidationError on bad data."""
    raw_stations = payload["data"]["stations"]
    stations = [model.model_validate(s) for s in raw_stations]
    digest = hashlib.sha256(
        json.dumps(raw_stations, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FeedSnapshot(
        last_updated=int(payload["last_updated"]),
        ttl=int(payload.get("ttl", 0)),
        stations=stations,
        content_hash=digest,
    )


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
