"""Planner inputs and outputs (see docs/DESIGN.md, "Move planner")."""

from __future__ import annotations

from dataclasses import dataclass, field

HANDLE_FIXED_MIN = 2.0
HANDLE_PER_BIKE_MIN = 0.25
SAFETY_BUFFER = 2


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float
    bikes: int  # bikes available now
    docks: int  # free docks now
    departures: float  # expected over the horizon
    arrivals: float  # expected over the horizon

    @property
    def capacity(self) -> int:
        return self.bikes + self.docks

    @property
    def projected(self) -> float:
        """Bikes at the end of the horizon if nobody moves anything."""
        return self.bikes + self.arrivals - self.departures


@dataclass(frozen=True)
class Params:
    vans: int = 3
    van_capacity: int = 25
    horizon_min: float = 60.0
    late_grace_min: float = 10.0
    max_receivers: int = 25
    max_donors: int = 25
    drive_cost_per_min: float = 0.01
    solver_time_limit_s: int = 25


@dataclass(frozen=True)
class Instance:
    stations: list[Station]
    depot: tuple[float, float]
    params: Params = field(default_factory=Params)
    label: str = ""

    def by_id(self) -> dict[str, Station]:
        return {s.id: s for s in self.stations}


@dataclass(frozen=True)
class Move:
    van: int
    order: int
    from_id: str
    to_id: str
    bikes: int
    pickup_at_min: float  # minutes after plan start, when loading finishes
    arrive_at_min: float  # when unloading finishes at the receiver
    deadline_min: float  # when the receiver is expected to run empty (or full)


@dataclass
class Plan:
    moves: list[Move]
    dropped: list[tuple[str, str, int, str]]  # (from, to, bikes, reason)
    solve_seconds: float
    status: str


def handling_minutes(bikes: int) -> float:
    return HANDLE_FIXED_MIN + HANDLE_PER_BIKE_MIN * bikes


def minutes_until_problem(s: Station, horizon: float) -> float:
    """When the station is expected to run empty (or full); inf if never within reach."""
    net_out = (s.departures - s.arrivals) / horizon  # bikes per minute
    if net_out > 0:
        return s.bikes / net_out
    if net_out < 0:
        return s.docks / -net_out
    return float("inf")


def shortage(s: Station, net_in: float = 0.0) -> float:
    return max(0.0, SAFETY_BUFFER - (s.projected + net_in))


def overflow(s: Station, net_in: float = 0.0) -> float:
    return max(0.0, (s.projected + net_in) - (s.capacity - SAFETY_BUFFER))
