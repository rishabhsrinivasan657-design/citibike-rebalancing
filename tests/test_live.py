"""Checks against the real feed. Run with `make test-live`."""

import httpx
import pytest

from rebalancer.config import Settings
from rebalancer.ingest.gbfs import (
    StationInformation,
    StationStatus,
    discover_feeds,
    fetch_json,
    parse_feed,
)

pytestmark = pytest.mark.live


def test_real_feeds_parse_cleanly():
    s = Settings()
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        feeds = discover_feeds(client, s.gbfs_discovery_url, s.gbfs_language)
        status = parse_feed(fetch_json(client, feeds["station_status"], 3).payload, StationStatus)
        info = parse_feed(
            fetch_json(client, feeds["station_information"], 3).payload, StationInformation
        )
    assert len(status.stations) > 1500
    status_ids = {x.station_id for x in status.stations}
    info_ids = {x.station_id for x in info.stations}
    assert len(status_ids & info_ids) / len(status_ids) > 0.98
    assert sum(1 for x in info.stations if x.short_name) / len(info.stations) > 0.98
