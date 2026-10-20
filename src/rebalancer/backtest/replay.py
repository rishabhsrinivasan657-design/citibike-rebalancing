"""Replay a recorded morning minute by minute under different van plans.

See docs/DESIGN.md, "Backtest (Step 6)". Index 0 of every per-minute list is the
state at the start of the window; index t (1..H) is the end of minute t.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rebalancer.planner.model import Move


@dataclass
class Replay:
    start_local: datetime
    minutes: int
    names: dict[str, str]
    bikes: dict[str, list[int]]  # recorded bikes available, length minutes + 1
    cap: dict[str, list[int]]  # bikes + free docks, length minutes + 1
    van: dict[str, list[int]]  # change during Lyft van visits (index 0 unused)
    hidden_dep: dict[str, list[float]]  # usual departures per minute (index 0 unused)
    hidden_arr: dict[str, list[float]]  # usual returns per minute (index 0 unused)
    recorder_gaps: int = 0

    @property
    def stations(self) -> list[str]:
        return sorted(self.bikes)

    def rider(self, s: str, t: int) -> int:
        """Recorded change in bikes at minute t that wasn't a van."""
        return self.bikes[s][t] - self.bikes[s][t - 1] - self.van[s][t]


@dataclass
class Outcome:
    missed_pickups: float = 0.0
    missed_returns: float = 0.0
    van_shortfall: float = 0.0  # planned bikes a van couldn't load or unload
    bikes: dict[str, list[float]] = field(default_factory=dict)

    @property
    def riders_affected(self) -> float:
        return self.missed_pickups + self.missed_returns


def moves_from_plan(rep: Replay, plan_moves: list[Move]) -> dict[str, list[int]]:
    """Pickups at the minute loading finishes, drop-offs when unloading finishes."""
    out = {s: [0] * (rep.minutes + 1) for s in rep.stations}
    for m in plan_moves:
        pick = max(1, math.ceil(m.pickup_at_min))
        drop = max(1, math.ceil(m.arrive_at_min))
        if drop <= rep.minutes and m.from_id in out and m.to_id in out:
            out[m.from_id][pick] -= m.bikes
            out[m.to_id][drop] += m.bikes
    return out


def simulate(rep: Replay, moves: dict[str, list[int]] | None, hidden: bool = True) -> Outcome:
    """Riders act first each minute, then vans (limited by what's physically there)."""
    out = Outcome()
    for s in rep.stations:
        b = float(rep.bikes[s][0])
        traj = [b]
        for t in range(1, rep.minutes + 1):
            cap = rep.cap[s][t]
            want = b + rep.rider(s, t)
            if hidden:
                if rep.bikes[s][t - 1] == 0:
                    want -= rep.hidden_dep[s][t]
                if rep.bikes[s][t - 1] >= rep.cap[s][t - 1]:
                    want += rep.hidden_arr[s][t]
            if want < 0:
                out.missed_pickups += -want
                want = 0.0
            elif want > cap:
                out.missed_returns += want - cap
                want = float(cap)
            m = moves[s][t] if moves else 0
            if m < 0:
                taken = min(-m, want)
                out.van_shortfall += -m - taken
                want -= taken
            elif m > 0:
                dropped = min(m, cap - want)
                out.van_shortfall += m - dropped
                want += dropped
            b = want
            traj.append(b)
        out.bikes[s] = traj
    return out


def max_replay_error(rep: Replay) -> float:
    """Replaying what actually happened, without hidden demand, must reproduce the
    recorded bike counts exactly. Returns the largest difference seen."""
    outcome = simulate(rep, rep.van, hidden=False)
    return max(
        abs(outcome.bikes[s][t] - rep.bikes[s][t])
        for s in rep.stations
        for t in range(rep.minutes + 1)
    )


def load(
    minute_path: Path,
    visits_path: Path,
    profile_path: Path,
    station_ids: list[str],
    short_names: dict[str, str],
    names: dict[str, str],
    start_local: datetime,
    minutes: int,
    day_type: str,
) -> Replay:
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TEMP TABLE ids (station_id VARCHAR)")
    con.executemany("INSERT INTO ids VALUES (?)", [(s,) for s in station_ids])
    t0 = f"timezone('America/New_York', TIMESTAMP '{start_local:%Y-%m-%d %H:%M:00}')"
    rows = con.execute(
        f"""SELECT m.station_id,
                   CAST(epoch(m.minute - {t0}) / 60 AS INTEGER) AS t,
                   m.num_bikes_available, m.num_bikes_available + m.num_docks_available,
                   m.recorder_up
            FROM read_parquet('{minute_path.as_posix()}') AS m JOIN ids USING (station_id)
            WHERE m.minute BETWEEN {t0} AND {t0} + INTERVAL {minutes} MINUTE
            ORDER BY 1, 2"""
    ).fetchall()
    visits = con.execute(
        f"""SELECT v.station_id,
                   CAST(epoch(v.started_minute - {t0}) / 60 AS INTEGER),
                   CAST(epoch(v.ended_minute - {t0}) / 60 AS INTEGER)
            FROM read_parquet('{visits_path.as_posix()}') AS v JOIN ids USING (station_id)
            WHERE v.ended_minute >= {t0}
              AND v.started_minute <= {t0} + INTERVAL {minutes} MINUTE"""
    ).fetchall()
    profile = con.execute(
        f"""SELECT station_id, hour, departures, arrivals
            FROM read_parquet('{profile_path.as_posix()}') WHERE day_type = ?""",
        [day_type],
    ).fetchall()
    con.close()

    n = minutes + 1
    bikes = {s: [None] * n for s in station_ids}
    cap = {s: [None] * n for s in station_ids}
    gaps = set()
    for sid, t, b, c, up in rows:
        bikes[sid][t], cap[sid][t] = b, c
        if not up:
            gaps.add(t)
    complete = [s for s in station_ids if None not in bikes[s]]
    bikes = {s: bikes[s] for s in complete}
    cap = {s: cap[s] for s in complete}

    van = {s: [0] * n for s in complete}
    for sid, a, b in visits:
        if sid in van:
            for t in range(max(1, a), min(minutes, b) + 1):
                van[sid][t] = bikes[sid][t] - bikes[sid][t - 1]

    rates: dict[tuple[str, int], tuple[float, float]] = {
        (r[0], r[1]): (r[2] / 60.0, r[3] / 60.0) for r in profile
    }
    hidden_dep = {s: [0.0] * n for s in complete}
    hidden_arr = {s: [0.0] * n for s in complete}
    for s in complete:
        for t in range(1, n):
            hour = (start_local.hour * 60 + start_local.minute + t - 1) // 60 % 24
            d, a = rates.get((short_names.get(s, ""), hour), (0.0, 0.0))
            hidden_dep[s][t], hidden_arr[s][t] = d, a

    return Replay(
        start_local,
        minutes,
        {s: names.get(s, s) for s in complete},
        bikes,
        cap,
        van,
        hidden_dep,
        hidden_arr,
        recorder_gaps=len(gaps),
    )
