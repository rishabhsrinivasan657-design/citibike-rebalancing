"""Polls the Citi Bike GBFS feeds and publishes to Kafka/Redpanda.

collector            # run forever
collector --once     # one status + information poll, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from rebalancer.config import Settings
from rebalancer.ingest.cdc import StatusChangeDetector
from rebalancer.ingest.gbfs import (
    StationInformation,
    StationStatus,
    discover_feeds,
    fetch_json,
    parse_feed,
)
from rebalancer.ingest.records import information_record, poll_log_record, status_record
from rebalancer.ingest.topics import ensure_topics

log = logging.getLogger("collector")


class Publisher(Protocol):
    def publish(self, topic: str, key: str | None, value: dict[str, Any]) -> None: ...
    def flush(self) -> None: ...


class KafkaPublisher:
    def __init__(self, bootstrap: str) -> None:
        from confluent_kafka import Producer

        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "enable.idempotence": True,
                "compression.type": "zstd",
                "linger.ms": 200,
            }
        )

    def publish(self, topic: str, key: str | None, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        while True:
            try:
                self._producer.produce(
                    topic,
                    value=payload,
                    key=key.encode() if key else None,
                    on_delivery=_on_delivery,
                )
                break
            except BufferError:
                self._producer.poll(0.5)
        self._producer.poll(0)

    def flush(self) -> None:
        remaining = self._producer.flush(30)
        if remaining:
            raise RuntimeError(f"{remaining} messages not delivered to Kafka")


def _on_delivery(err: Any, msg: Any) -> None:
    if err is not None:
        log.error("delivery failed for %s: %s", msg.topic(), err)


class Collector:
    def __init__(self, settings: Settings, client: httpx.Client, publisher: Publisher) -> None:
        self.s = settings
        self.client = client
        self.pub = publisher
        self.detector = StatusChangeDetector(settings.status_heartbeat_s)
        self.feeds: dict[str, str] = {}
        self._info_hash: str | None = None
        self._info_published_at = 0.0

    def resolve_feeds(self) -> None:
        self.feeds = discover_feeds(self.client, self.s.gbfs_discovery_url, self.s.gbfs_language)
        log.info(
            "resolved feeds: status=%s info=%s",
            self.feeds["station_status"],
            self.feeds["station_information"],
        )

    def poll_status(self) -> int:
        """Poll station_status once. Returns the number of changed stations published."""
        polled_at = _now_ms()
        fetch = fetch_json(self.client, self.feeds["station_status"], self.s.http_retries)
        if not fetch.ok:
            self._log_poll("station_status", polled_at, fetch)
            return 0
        try:
            snap = parse_feed(fetch.payload, StationStatus)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            self._log_poll("station_status", polled_at, fetch, error=f"parse: {exc}"[:2000])
            return 0

        changed = self.detector.changed(snap.stations, now=polled_at / 1000)
        for s in changed:
            self.pub.publish(
                self.s.topic_station_status,
                s.station_id,
                status_record(s, snap.last_updated, polled_at),
            )
        self._log_poll(
            "station_status", polled_at, fetch, snap.last_updated, len(snap.stations), len(changed)
        )
        self.pub.flush()
        return len(changed)

    def poll_information(self, force: bool = False) -> bool:
        """Poll station_information. Publishes a full snapshot if content changed or is stale."""
        polled_at = _now_ms()
        fetch = fetch_json(self.client, self.feeds["station_information"], self.s.http_retries)
        if not fetch.ok:
            self._log_poll("station_information", polled_at, fetch)
            return False
        try:
            snap = parse_feed(fetch.payload, StationInformation)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            self._log_poll("station_information", polled_at, fetch, error=f"parse: {exc}"[:2000])
            return False

        stale = time.time() - self._info_published_at >= self.s.info_max_age_s
        publish = force or stale or snap.content_hash != self._info_hash
        if publish:
            for s in snap.stations:
                self.pub.publish(
                    self.s.topic_station_information,
                    s.station_id,
                    information_record(s, snap.last_updated, polled_at),
                )
            self._info_hash = snap.content_hash
            self._info_published_at = time.time()
        self._log_poll(
            "station_information",
            polled_at,
            fetch,
            snap.last_updated,
            len(snap.stations),
            len(snap.stations) if publish else 0,
        )
        self.pub.flush()
        return publish

    def _log_poll(
        self,
        feed: str,
        polled_at: int,
        fetch,
        feed_last_updated=None,
        n_stations=None,
        n_changed=None,
        error=None,
    ) -> None:
        rec = poll_log_record(
            feed, polled_at, fetch, feed_last_updated, n_stations, n_changed, error
        )
        self.pub.publish(self.s.topic_poll_log, feed, rec)
        level = logging.INFO if rec["ok"] else logging.ERROR
        log.log(
            level,
            "poll %s ok=%s stations=%s changed=%s latency=%sms%s",
            feed,
            rec["ok"],
            n_stations,
            n_changed,
            fetch.latency_ms,
            f" error={rec['error']}" if rec["error"] else "",
        )

    def run(self, stop: StopFlag) -> None:
        delay = 5.0
        while not self.feeds:
            try:
                self.resolve_feeds()
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.error("feed discovery failed, retrying in %.0fs: %s", delay, exc)
                stop.wait(delay)
                if stop.set:
                    return
                delay = min(delay * 2, 120)
        next_status = next_info = time.time()
        while not stop.set:
            now = time.time()
            if now >= next_info:
                self.poll_information()
                next_info += self.s.info_interval_s
            if now >= next_status:
                self.poll_status()
                # Stay on a fixed cadence; if we fell behind, skip ahead instead of bursting.
                next_status += self.s.poll_interval_s
                if next_status < time.time():
                    next_status = time.time() + self.s.poll_interval_s
            stop.wait(max(0.0, min(next_status, next_info) - time.time()))


class StopFlag:
    """Set by a signal handler, or automatically once `deadline` (epoch seconds) passes."""

    def __init__(self, deadline: float | None = None) -> None:
        self._set = False
        self.deadline = deadline

    @property
    def set(self) -> bool:
        return self._set or (self.deadline is not None and time.time() >= self.deadline)

    def __call__(self, *_: Any) -> None:
        log.info("shutdown requested")
        self._set = True

    def wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.set and time.time() < end:
            time.sleep(min(0.5, end - time.time()))


def _now_ms() -> int:
    return int(time.time() * 1000)


def http_client(settings: Settings) -> httpx.Client:
    headers = {"User-Agent": "citibike-rebalancing research collector"}
    return httpx.Client(timeout=settings.http_timeout_s, headers=headers, follow_redirects=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll each feed once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings()
    ensure_topics(settings)
    with http_client(settings) as client:
        collector = Collector(settings, client, KafkaPublisher(settings.kafka_bootstrap))
        if args.once:
            collector.resolve_feeds()
            collector.poll_information(force=True)
            collector.poll_status()
            return
        stop = StopFlag()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        collector.run(stop)


if __name__ == "__main__":
    main()
