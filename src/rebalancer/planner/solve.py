"""Stage 1: choose how many bikes to move between which stations (integer program)."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

import pulp

from rebalancer.planner.geo import drive_minutes
from rebalancer.planner.model import (
    SAFETY_BUFFER,
    Instance,
    Station,
    handling_minutes,
    minutes_until_problem,
    overflow,
    shortage,
)


@dataclass(frozen=True)
class Transfer:
    from_id: str
    to_id: str
    bikes: int
    trips: int


def urgency_weight(s: Station, horizon: float) -> float:
    t = minutes_until_problem(s, horizon)
    return min(6.0, max(1.0, horizon / max(t, 1.0)))


def candidates(inst: Instance) -> tuple[list[Station], list[Station]]:
    """Receivers: projected short, most urgent first. Donors: can spare bikes,
    overflowing stations first. Each list is capped to keep the model small."""
    p = inst.params
    h = p.horizon_min
    receivers = sorted(
        (s for s in inst.stations if shortage(s) > 0 and s.docks > 0),
        key=lambda s: minutes_until_problem(s, h),
    )[: p.max_receivers]
    taken = {s.id for s in receivers}
    donors = sorted(
        (
            s
            for s in inst.stations
            if s.id not in taken and s.bikes > 0 and s.projected - SAFETY_BUFFER > 0
        ),
        key=lambda s: (-overflow(s), -(s.projected - SAFETY_BUFFER)),
    )[: p.max_donors]
    return receivers, donors


def choose_transfers(inst: Instance) -> tuple[list[Transfer], str]:
    p = inst.params
    h = p.horizon_min
    lat0, lon0 = inst.depot
    receivers, donors = candidates(inst)
    if not receivers and not any(overflow(s) > 0 for s in donors):
        return [], "Nothing to fix"

    def loc(s: Station) -> tuple[float, float]:
        return (s.lat, s.lon)

    tau = {
        (i.id, j.id): drive_minutes(loc(i), loc(j), lat0, lon0) for i in donors for j in receivers
    }
    # Overflowing donors may also send bikes to non-short stations with room to spare.
    sinks = [s for s in inst.stations if s not in receivers and s.docks > 0 and overflow(s) == 0]
    if any(overflow(s) > 0 for s in donors):
        sinks = sorted(sinks, key=lambda s: -(s.capacity - SAFETY_BUFFER - s.projected))[:10]
        for i in donors:
            for j in sinks:
                tau[(i.id, j.id)] = drive_minutes(loc(i), loc(j), lat0, lon0)
    else:
        sinks = []
    targets = receivers + sinks

    deadhead = 0.5 * statistics.median(tau.values()) if tau else 0.0
    by_id = inst.by_id()

    load = handling_minutes(p.van_capacity // 2)

    def earliest_arrival(i: Station, j: Station) -> float:
        """Optimistic: a van driving straight from the depot. Stage 2 uses real times."""
        return drive_minutes(inst.depot, loc(i), lat0, lon0) + load + tau[(i.id, j.id)] + load

    def useful(i: Station, j: Station) -> float:
        """Bikes a delivery to j can still help with if it arrives at the earliest time.
        Riders who found j empty before the bikes arrived can't be helped."""
        if j in sinks:
            return max(0.0, j.capacity - SAFETY_BUFFER - j.projected)
        rate = max(0.0, (j.departures - j.arrivals) / h)
        a = earliest_arrival(i, j)
        return rate * max(0.0, h - max(a, minutes_until_problem(j, h))) + SAFETY_BUFFER

    pairs = [
        (i.id, j.id)
        for i in donors
        for j in targets
        if i.id != j.id
        and (j not in sinks or overflow(i) > 0)  # only overflowing stations unload into sinks
        and earliest_arrival(i, j) <= h
        and useful(i, j) >= 1
    ]
    if not pairs:
        return [], "No move can arrive in time"
    cap = {pr: math.floor(useful(by_id[pr[0]], by_id[pr[1]])) for pr in pairs}

    m = pulp.LpProblem("rebalance", pulp.LpMinimize)
    # Bikes are continuous here and rounded down afterwards (rounding down can't break a
    # limit); only trip counts must be whole numbers. Much faster to solve.
    x = {pr: pulp.LpVariable(f"x_{n}", 0) for n, pr in enumerate(pairs)}
    z = {pr: pulp.LpVariable(f"z_{n}", 0, cat="Integer") for n, pr in enumerate(pairs)}
    involved = {i for pr in pairs for i in pr}
    short = {i: pulp.LpVariable(f"short_{k}", 0) for k, i in enumerate(involved)}
    over = {i: pulp.LpVariable(f"over_{k}", 0) for k, i in enumerate(involved)}

    def flow_in(sid: str):
        return pulp.lpSum(x[pr] for pr in pairs if pr[1] == sid)

    def flow_out(sid: str):
        return pulp.lpSum(x[pr] for pr in pairs if pr[0] == sid)

    weights = {sid: urgency_weight(by_id[sid], h) for sid in involved}
    m += pulp.lpSum(weights[s] * (short[s] + over[s]) for s in involved) + p.drive_cost_per_min * (
        pulp.lpSum(tau[pr] * z[pr] for pr in pairs)
    )

    for pr in pairs:
        m += x[pr] <= p.van_capacity * z[pr]
        m += x[pr] <= cap[pr]
        m += z[pr] <= math.ceil(cap[pr] / p.van_capacity)  # no pointless empty trips
    for sid in involved:
        st = by_id[sid]
        end = st.projected + flow_in(sid) - flow_out(sid)
        m += flow_out(sid) <= st.bikes
        m += flow_in(sid) <= st.docks
        m += short[sid] >= SAFETY_BUFFER - end
        m += over[sid] >= end - (st.capacity - SAFETY_BUFFER)
    per_trip = handling_minutes(p.van_capacity) * 2 + deadhead
    m += pulp.lpSum(z[pr] * (tau[pr] + per_trip) for pr in pairs) <= p.vans * h

    # Accept a plan proven within 1% of the best possible; proving the last 1% can take
    # far longer and doesn't change the moves in a way that matters.
    m.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=p.solver_time_limit_s, gapRel=0.01))
    # PuLP reports "Optimal" even when CBC stops at the time limit, so use sol_status.
    status = {
        pulp.LpSolutionOptimal: "Optimal (within 1%)",
        pulp.LpSolutionIntegerFeasible: "Best found before the time limit",
    }.get(m.sol_status, pulp.LpStatus[m.status])

    out = []
    for pr in pairs:
        bikes = math.floor((x[pr].value() or 0) + 1e-6)
        if bikes > 0:
            trips = max(1, math.ceil(bikes / p.van_capacity))
            out.append(Transfer(pr[0], pr[1], bikes, trips))
    return out, status
