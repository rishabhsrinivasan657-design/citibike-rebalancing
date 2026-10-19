"""Consumes GBFS topics from Kafka/Redpanda and writes them to the Parquet lake.

At-least-once: offsets are committed only after the batch's files are written.

    lake-sink                    # run forever
    lake-sink --drain-seconds 20 # consume until idle for 20 s, flush, exit
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from typing import Any, Protocol

from rebalancer.config import Settings
from rebalancer.ingest.lake import LakeWriter
from rebalancer.ingest.topics import ensure_topics

log = logging.getLogger("lake-sink")


class Message(Protocol):
    def topic(self) -> str: ...
    def value(self) -> bytes: ...
    def error(self) -> Any: ...


class BatchBuffer:
    """Accumulates decoded records per lake table and decides when to flush."""

    def __init__(self, topic_to_table: dict[str, str], flush_interval_s: float, max_rows: int):
        self.topic_to_table = topic_to_table
        self.flush_interval_s = flush_interval_s
        self.max_rows = max_rows
        self.rows: dict[str, list[dict[str, Any]]] = {t: [] for t in topic_to_table.values()}
        self.started = time.monotonic()
        self.bad_messages = 0

    def add(self, msg: Message) -> None:
        table = self.topic_to_table[msg.topic()]
        try:
            self.rows[table].append(json.loads(msg.value()))
        except (json.JSONDecodeError, TypeError) as exc:
            self.bad_messages += 1
            log.error("skipping undecodable message on %s: %s", msg.topic(), exc)

    def __len__(self) -> int:
        return sum(len(v) for v in self.rows.values())

    def due(self) -> bool:
        return len(self) >= self.max_rows or (
            len(self) > 0 and time.monotonic() - self.started >= self.flush_interval_s
        )

    def drain(self) -> dict[str, list[dict[str, Any]]]:
        out = {t: rows for t, rows in self.rows.items() if rows}
        self.rows = {t: [] for t in self.rows}
        self.started = time.monotonic()
        return out


def flush(buffer: BatchBuffer, writer: LakeWriter, commit) -> int:
    """Write buffered rows, then commit offsets. Returns the number of rows written."""
    batch = buffer.drain()
    n = 0
    for table, rows in batch.items():
        paths = writer.write(table, rows)
        n += len(rows)
        log.info("wrote %d rows to %s (%d files)", len(rows), table, len(paths))
    if n:
        commit()
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drain-seconds",
        type=float,
        default=None,
        help="exit after this many seconds with no new messages",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from confluent_kafka import Consumer, KafkaError, KafkaException

    s = Settings()
    ensure_topics(s)
    topic_to_table = {
        s.topic_station_status: "station_status",
        s.topic_station_information: "station_information",
        s.topic_poll_log: "poll_log",
    }
    consumer = Consumer(
        {
            "bootstrap.servers": s.kafka_bootstrap,
            "group.id": s.sink_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    writer = LakeWriter(s.lake_uri)
    buffer = BatchBuffer(topic_to_table, s.sink_flush_interval_s, s.sink_max_buffer_rows)

    def commit() -> None:
        try:
            consumer.commit(asynchronous=False)
        except KafkaException as exc:
            # Nothing consumed since the last commit (e.g. right after a rebalance).
            if exc.args[0].code() != KafkaError._NO_OFFSET:
                raise

    def on_revoke(_consumer: Any, _partitions: Any) -> None:
        # Write and commit what we hold before losing the partitions, so the next
        # owner doesn't re-read it and we don't commit offsets we no longer own.
        flush(buffer, writer, commit)

    consumer.subscribe(list(topic_to_table), on_revoke=on_revoke)

    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_msg = time.monotonic()
    try:
        while not stopping:
            msg = consumer.poll(1.0)
            if msg is None:
                if (
                    args.drain_seconds is not None
                    and time.monotonic() - last_msg > args.drain_seconds
                ):
                    break
            elif msg.error():
                log.error("consumer error: %s", msg.error())
            else:
                buffer.add(msg)
                last_msg = time.monotonic()
            if buffer.due():
                flush(buffer, writer, commit)
        flush(buffer, writer, commit)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
