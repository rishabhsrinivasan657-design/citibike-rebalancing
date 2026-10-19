import time

import httpx
import pyarrow.dataset as ds
import pytest
import respx

from rebalancer.config import Settings
from rebalancer.ingest.collector import Collector, StopFlag
from rebalancer.ingest.recorder import HuggingFaceUploader, LakePublisher

STATUS_URL = "https://example.test/station_status.json"
INFO_URL = "https://example.test/station_information.json"


class FakeUploader:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def upload(self, files, root) -> None:
        if self.fail:
            raise ConnectionError("hub down")
        self.calls.append([f.relative_to(root).as_posix() for f in files])


def _publisher(tmp_path, uploader, persist_every_s=0.0):
    return LakePublisher(Settings(), tmp_path, uploader, persist_every_s)


@respx.mock
def test_poll_writes_parquet_and_uploads(tmp_path, status_payload, info_payload):
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=status_payload))
    respx.get(INFO_URL).mock(return_value=httpx.Response(200, json=info_payload))
    up = FakeUploader()
    pub = _publisher(tmp_path, up)
    c = Collector(Settings(http_retries=1), httpx.Client(), pub)
    c.feeds = {"station_status": STATUS_URL, "station_information": INFO_URL}

    c.poll_information()
    c.poll_status()

    uploaded = [p for call in up.calls for p in call]
    assert any(p.startswith("station_status/date=") for p in uploaded)
    assert any(p.startswith("station_information/date=") for p in uploaded)
    assert any(p.startswith("poll_log/date=") for p in uploaded)
    assert pub.pending_files() == []  # deleted locally after a successful upload


def test_failed_upload_keeps_files_and_retries(tmp_path):
    up = FakeUploader(fail=True)
    pub = _publisher(tmp_path, up)
    pub.publish(
        "gbfs.poll_log",
        "station_status",
        {
            "polled_at": int(time.time() * 1000),
            "feed": "station_status",
            "ok": True,
            "http_status": 200,
            "latency_ms": 40,
            "feed_last_updated": None,
            "n_stations": 1,
            "n_changed": 1,
            "error": None,
        },
    )
    pub.persist()
    pending = pub.pending_files()
    assert len(pending) == 1
    assert ds.dataset(pending[0]).count_rows() == 1

    up.fail = False
    pub.persist()  # nothing new buffered, but the leftover file is retried
    assert len(up.calls) == 1 and pub.pending_files() == []


def test_flush_waits_for_persist_interval(tmp_path):
    up = FakeUploader()
    pub = _publisher(tmp_path, up, persist_every_s=3600)
    pub.publish("gbfs.poll_log", "x", {"polled_at": 1792418370_000, "feed": "x", "ok": True})
    pub.flush()
    assert up.calls == [] and pub.pending_files() == []
    assert len(pub.buffer["poll_log"]) == 1


def test_stop_flag_deadline():
    assert StopFlag(deadline=time.time() - 1).set
    flag = StopFlag(deadline=time.time() + 60)
    assert not flag.set
    flag()
    assert flag.set


@respx.mock
def test_run_retries_discovery_then_stops_at_deadline(tmp_path):
    settings = Settings(gbfs_discovery_url="https://example.test/gbfs.json")
    route = respx.get("https://example.test/gbfs.json").mock(return_value=httpx.Response(503))
    c = Collector(settings, httpx.Client(), _publisher(tmp_path, None))
    started = time.time()
    c.run(StopFlag(deadline=time.time() + 1))
    assert route.call_count >= 1 and time.time() - started < 10
    assert c.feeds == {}


def test_hf_uploader_maps_paths(tmp_path):
    class FakeApi:
        def create_repo(self, *a, **kw):
            self.created = kw

        def create_commit(self, **kw):
            self.commit = kw

    f = tmp_path / "poll_log" / "date=2026-10-19" / "hour=13" / "part-1.parquet"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    api = FakeApi()
    HuggingFaceUploader("me/citibike", "tok", api=api).upload([f], tmp_path)
    assert api.created["private"] is True
    (op,) = api.commit["operations"]
    assert op.path_in_repo == "lake/poll_log/date=2026-10-19/hour=13/part-1.parquet"


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    # fetch_json backs off with time.sleep between retries; keep tests quick.
    monkeypatch.setattr("rebalancer.ingest.gbfs.time.sleep", lambda s: None)
