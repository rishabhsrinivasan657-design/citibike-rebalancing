"""Message payloads published to Kafka (see the data contracts in docs/DESIGN.md).

Times travel as integers: `last_reported` and `feed_last_updated` in epoch seconds
(as GBFS gives them), `polled_at` in epoch milliseconds. The lake sink converts all
of them to UTC timestamps.
"""

from __future__ import annotations

from typing import Any

from rebalancer.ingest.gbfs import FetchResult, StationInformation, StationStatus


def status_record(s: StationStatus, feed_last_updated: int, polled_at_ms: int) -> dict[str, Any]:
    return {**s.model_dump(), "feed_last_updated": feed_last_updated, "polled_at": polled_at_ms}


def information_record(
    s: StationInformation, feed_last_updated: int, polled_at_ms: int
) -> dict[str, Any]:
    return {**s.model_dump(), "feed_last_updated": feed_last_updated, "polled_at": polled_at_ms}


def poll_log_record(
    feed: str,
    polled_at_ms: int,
    fetch: FetchResult,
    feed_last_updated: int | None = None,
    n_stations: int | None = None,
    n_changed: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "polled_at": polled_at_ms,
        "feed": feed,
        "ok": fetch.ok and error is None,
        "http_status": fetch.http_status,
        "latency_ms": fetch.latency_ms,
        "feed_last_updated": feed_last_updated,
        "n_stations": n_stations,
        "n_changed": n_changed,
        "error": error or fetch.error,
    }
