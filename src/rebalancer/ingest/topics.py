"""Explicit topic creation, so consumers never subscribe to topics that don't exist yet."""

from __future__ import annotations

import logging

from rebalancer.config import Settings

log = logging.getLogger(__name__)

RETENTION_MS = 7 * 24 * 3600 * 1000


def topic_names(s: Settings) -> list[str]:
    return [s.topic_station_status, s.topic_station_information, s.topic_poll_log]


def ensure_topics(s: Settings) -> None:
    """Create the GBFS topics if missing. Safe to call from every service at startup."""
    from confluent_kafka import KafkaError, KafkaException
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": s.kafka_bootstrap})
    new = [
        NewTopic(
            t, num_partitions=1, replication_factor=1, config={"retention.ms": str(RETENTION_MS)}
        )
        for t in topic_names(s)
    ]
    for topic, future in admin.create_topics(new).items():
        try:
            future.result(timeout=30)
            log.info("created topic %s", topic)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
