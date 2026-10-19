import httpx
import respx

from rebalancer.config import Settings
from rebalancer.ingest.collector import Collector

STATUS_URL = "https://example.test/station_status.json"
INFO_URL = "https://example.test/station_information.json"


def _collector(publisher) -> Collector:
    c = Collector(Settings(http_retries=1), httpx.Client(), publisher)
    c.feeds = {"station_status": STATUS_URL, "station_information": INFO_URL}
    return c


@respx.mock
def test_status_poll_publishes_changes_and_poll_log(publisher, status_payload):
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=status_payload))
    c = _collector(publisher)

    assert c.poll_status() == 5
    assert c.poll_status() == 0
    assert len(publisher.on("gbfs.station_status")) == 5
    logs = publisher.on("gbfs.poll_log")
    assert [entry["n_changed"] for entry in logs] == [5, 0]
    assert all(entry["ok"] for entry in logs)
    rec = publisher.on("gbfs.station_status")[0]
    assert rec["feed_last_updated"] == status_payload["last_updated"]
    assert rec["polled_at"] > 1_700_000_000_000


@respx.mock
def test_http_failure_is_logged_not_raised(publisher):
    respx.get(STATUS_URL).mock(return_value=httpx.Response(503))
    assert _collector(publisher).poll_status() == 0
    (entry,) = publisher.on("gbfs.poll_log")
    assert entry["ok"] is False and entry["http_status"] == 503


@respx.mock
def test_bad_payload_is_logged_not_raised(publisher, status_payload):
    del status_payload["data"]["stations"][0]["station_id"]
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=status_payload))
    assert _collector(publisher).poll_status() == 0
    (entry,) = publisher.on("gbfs.poll_log")
    assert entry["ok"] is False and entry["error"].startswith("parse:")
    assert publisher.on("gbfs.station_status") == []


@respx.mock
def test_information_published_only_on_change(publisher, info_payload):
    route = respx.get(INFO_URL).mock(return_value=httpx.Response(200, json=info_payload))
    c = _collector(publisher)
    assert c.poll_information() is True
    assert c.poll_information() is False
    info_payload["data"]["stations"][0]["capacity"] += 2
    route.mock(return_value=httpx.Response(200, json=info_payload))
    assert c.poll_information() is True
    assert len(publisher.on("gbfs.station_information")) == 10
