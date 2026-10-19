"""Change detection for station status.

Most stations re-report every minute with identical counts (a heartbeat), so keying
on `last_reported` alone keeps ~80% redundant rows. A station is emitted when:

- it is seen for the first time (after a restart this yields a full snapshot),
- any inventory or operating-state field changed, or
- it hasn't been emitted for `heartbeat_s`, so downstream can tell "unchanged"
  apart from "stopped reporting".
"""

from __future__ import annotations

from rebalancer.ingest.gbfs import StationStatus

STATE_FIELDS = (
    "num_bikes_available",
    "num_ebikes_available",
    "num_bikes_disabled",
    "num_docks_available",
    "num_docks_disabled",
    "is_installed",
    "is_renting",
    "is_returning",
)


class StatusChangeDetector:
    def __init__(self, heartbeat_s: int = 900) -> None:
        self.heartbeat_s = heartbeat_s
        self._state: dict[str, tuple] = {}
        self._emitted_at: dict[str, float] = {}

    def changed(self, stations: list[StationStatus], now: float) -> list[StationStatus]:
        out = []
        for s in stations:
            state = tuple(getattr(s, f) for f in STATE_FIELDS)
            last = self._emitted_at.get(s.station_id)
            if last is None or self._state[s.station_id] != state or now - last >= self.heartbeat_s:
                self._state[s.station_id] = state
                self._emitted_at[s.station_id] = now
                out.append(s)
        return out

    def __len__(self) -> int:
        return len(self._state)
