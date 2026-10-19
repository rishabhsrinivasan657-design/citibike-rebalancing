"""Partitioned Parquet writer for the raw lake.

Layout: <lake_uri>/<table>/date=YYYY-MM-DD/hour=HH/part-<unix_ms>-<uuid8>.parquet,
partitioned by `polled_at` in UTC. Files are written to a temporary name and then
renamed, so readers never see a half-written file on a local filesystem.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

TS = pa.timestamp("ms", tz="UTC")

SCHEMAS: dict[str, pa.Schema] = {
    "station_status": pa.schema(
        [
            ("station_id", pa.string()),
            ("legacy_id", pa.string()),
            ("num_bikes_available", pa.int32()),
            ("num_ebikes_available", pa.int32()),
            ("num_bikes_disabled", pa.int32()),
            ("num_docks_available", pa.int32()),
            ("num_docks_disabled", pa.int32()),
            ("is_installed", pa.bool_()),
            ("is_renting", pa.bool_()),
            ("is_returning", pa.bool_()),
            ("last_reported", TS),
            ("feed_last_updated", TS),
            ("polled_at", TS),
        ]
    ),
    "station_information": pa.schema(
        [
            ("station_id", pa.string()),
            ("short_name", pa.string()),
            ("name", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("region_id", pa.string()),
            ("capacity", pa.int32()),
            ("station_type", pa.string()),
            ("has_kiosk", pa.bool_()),
            ("feed_last_updated", TS),
            ("polled_at", TS),
        ]
    ),
    "poll_log": pa.schema(
        [
            ("polled_at", TS),
            ("feed", pa.string()),
            ("ok", pa.bool_()),
            ("http_status", pa.int32()),
            ("latency_ms", pa.int32()),
            ("feed_last_updated", TS),
            ("n_stations", pa.int32()),
            ("n_changed", pa.int32()),
            ("error", pa.string()),
        ]
    ),
}

# Fields that arrive as epoch seconds; everything else timestamp-typed is epoch ms.
_EPOCH_SECONDS = {"last_reported", "feed_last_updated"}


class LakeWriter:
    def __init__(self, lake_uri: str) -> None:
        if "://" not in lake_uri:
            lake_uri = Path(lake_uri).resolve().as_uri()
        self.fs, self.root = pafs.FileSystem.from_uri(lake_uri)

    def write(self, table: str, records: list[dict[str, Any]]) -> list[str]:
        """Write records into hour partitions. Returns the paths written."""
        if table not in SCHEMAS:
            raise KeyError(f"unknown table {table!r}")
        by_partition: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            ts = datetime.fromtimestamp(r["polled_at"] / 1000, tz=UTC)
            by_partition[(ts.strftime("%Y-%m-%d"), ts.strftime("%H"))].append(r)

        paths = []
        for (date, hour), rows in sorted(by_partition.items()):
            arrow = to_arrow(table, rows)
            directory = f"{self.root}/{table}/date={date}/hour={hour}"
            self.fs.create_dir(directory, recursive=True)
            name = f"part-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.parquet"
            final, tmp = f"{directory}/{name}", f"{directory}/.{name}.tmp"
            pq.write_table(arrow, tmp, filesystem=self.fs, compression="zstd")
            self.fs.move(tmp, final)
            paths.append(final)
        return paths


def to_arrow(table: str, rows: list[dict[str, Any]]) -> pa.Table:
    schema = SCHEMAS[table]
    columns = {}
    for field in schema:
        values = [r.get(field.name) for r in rows]
        if field.type == TS:
            scale = 1000 if field.name in _EPOCH_SECONDS else 1
            values = [None if v is None else v * scale for v in values]
        columns[field.name] = pa.array(values, type=field.type)
    return pa.table(columns, schema=schema)
