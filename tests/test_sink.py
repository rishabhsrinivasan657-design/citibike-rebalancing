import json

import pyarrow.dataset as ds
import pytest

from rebalancer.ingest.lake import LakeWriter
from rebalancer.ingest.sink import BatchBuffer, flush


class Msg:
    def __init__(self, topic, value):
        self._t, self._v = topic, value

    def topic(self):
        return self._t

    def value(self):
        return self._v

    def error(self):
        return None


def _log(polled_at):
    return json.dumps(
        {
            "polled_at": polled_at,
            "feed": "station_status",
            "ok": True,
            "http_status": 200,
            "latency_ms": 120,
            "feed_last_updated": 1792418360,
            "n_stations": 2519,
            "n_changed": 40,
            "error": None,
        }
    ).encode()


def test_flush_writes_then_commits(tmp_path):
    buf = BatchBuffer({"gbfs.poll_log": "poll_log"}, flush_interval_s=999, max_rows=2)
    buf.add(Msg("gbfs.poll_log", _log(1792418370_000)))
    assert not buf.due()
    buf.add(Msg("gbfs.poll_log", b"{not json"))
    buf.add(Msg("gbfs.poll_log", _log(1792418430_000)))
    assert buf.due() and buf.bad_messages == 1

    commits = []
    n = flush(buf, LakeWriter(str(tmp_path)), lambda: commits.append(1))
    assert n == 2 and commits == [1] and len(buf) == 0
    assert ds.dataset(tmp_path / "poll_log", partitioning="hive").count_rows() == 2


def test_no_commit_when_write_fails(tmp_path):
    class Boom(LakeWriter):
        def write(self, table, records):
            raise OSError("disk full")

    buf = BatchBuffer({"gbfs.poll_log": "poll_log"}, flush_interval_s=0, max_rows=10)
    buf.add(Msg("gbfs.poll_log", _log(1792418370_000)))
    commits = []
    with pytest.raises(OSError):
        flush(buf, Boom(str(tmp_path)), lambda: commits.append(1))
    assert commits == []
