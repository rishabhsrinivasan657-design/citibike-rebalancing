import copy

import pytest
from pydantic import ValidationError

from rebalancer.ingest.gbfs import StationInformation, StationStatus, parse_feed


def test_parse_status_fixture(status_payload):
    snap = parse_feed(status_payload, StationStatus)
    assert len(snap.stations) == 5
    assert snap.ttl == 60
    s = snap.stations[0]
    assert isinstance(s.is_renting, bool)
    assert s.num_docks_available >= 0


def test_parse_information_fixture(info_payload):
    snap = parse_feed(info_payload, StationInformation)
    s = snap.stations[0]
    assert s.short_name and s.capacity > 0
    assert 40 < s.lat < 41.5 and -75 < s.lon < -73


def test_unknown_fields_are_ignored(status_payload):
    status_payload["data"]["stations"][0]["brand_new_field"] = 1
    parse_feed(status_payload, StationStatus)


def test_missing_required_field_fails(status_payload):
    del status_payload["data"]["stations"][0]["num_bikes_available"]
    with pytest.raises(ValidationError):
        parse_feed(status_payload, StationStatus)


def test_content_hash_tracks_content(info_payload):
    a = parse_feed(info_payload, StationInformation).content_hash
    changed = copy.deepcopy(info_payload)
    changed["data"]["stations"][0]["capacity"] += 1
    assert parse_feed(info_payload, StationInformation).content_hash == a
    assert parse_feed(changed, StationInformation).content_hash != a
