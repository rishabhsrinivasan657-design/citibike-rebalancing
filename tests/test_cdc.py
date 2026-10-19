from rebalancer.ingest.cdc import StatusChangeDetector
from rebalancer.ingest.gbfs import StationStatus, parse_feed


def _stations(payload):
    return parse_feed(payload, StationStatus).stations


def test_first_poll_emits_everything(status_payload):
    det = StatusChangeDetector()
    assert len(det.changed(_stations(status_payload), now=0)) == 5
    assert len(det) == 5


def test_heartbeat_only_report_is_suppressed(status_payload):
    det = StatusChangeDetector(heartbeat_s=900)
    det.changed(_stations(status_payload), now=0)
    for s in status_payload["data"]["stations"]:
        s["last_reported"] += 60
    assert det.changed(_stations(status_payload), now=60) == []


def test_inventory_change_is_emitted(status_payload):
    det = StatusChangeDetector()
    det.changed(_stations(status_payload), now=0)
    status_payload["data"]["stations"][2]["num_docks_available"] += 1
    changed = det.changed(_stations(status_payload), now=60)
    assert [s.station_id for s in changed] == [status_payload["data"]["stations"][2]["station_id"]]


def test_heartbeat_reemits_unchanged_stations(status_payload):
    det = StatusChangeDetector(heartbeat_s=900)
    det.changed(_stations(status_payload), now=0)
    assert det.changed(_stations(status_payload), now=899) == []
    assert len(det.changed(_stations(status_payload), now=900)) == 5
