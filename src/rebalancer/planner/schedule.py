"""Stage 2: turn chosen transfers into ordered van trips with real arrival times,
then plan() ties both stages together and verifies the result."""

from __future__ import annotations

import time

from rebalancer.planner.geo import drive_minutes
from rebalancer.planner.model import (
    Instance,
    Move,
    Plan,
    handling_minutes,
    minutes_until_problem,
)
from rebalancer.planner.solve import Transfer, choose_transfers


def schedule(inst: Instance, transfers: list[Transfer]) -> Plan:
    p = inst.params
    h = p.horizon_min
    lat0, lon0 = inst.depot
    by_id = inst.by_id()

    trips: list[tuple[float, str, str, int]] = []  # (deadline, from, to, bikes)
    for t in transfers:
        left = t.bikes
        for _ in range(t.trips):
            q = min(left, p.van_capacity)
            if q <= 0:
                break
            deadline = min(h, minutes_until_problem(by_id[t.to_id], h))
            trips.append((deadline, t.from_id, t.to_id, q))
            left -= q
    trips.sort()

    van_free = [0.0] * p.vans
    van_at = [inst.depot] * p.vans
    van_count = [0] * p.vans
    taken = dict.fromkeys(by_id, 0)  # bikes removed from each station so far
    given = dict.fromkeys(by_id, 0)  # bikes delivered to each station so far
    moves: list[Move] = []
    dropped: list[tuple[str, str, int, str]] = []

    for deadline, a, b, q in trips:
        sa, sb = by_id[a], by_id[b]
        q = min(q, sa.bikes - taken[a], sb.docks - given[b])
        if q <= 0:
            dropped.append((a, b, 0, "no bikes or docks left"))
            continue
        best = None
        for v in range(p.vans):
            pick = (
                van_free[v]
                + drive_minutes(van_at[v], (sa.lat, sa.lon), lat0, lon0)
                + handling_minutes(q)
            )
            arrive = pick + drive_minutes((sa.lat, sa.lon), (sb.lat, sb.lon), lat0, lon0)
            arrive += handling_minutes(q)
            if best is None or arrive < best[0]:
                best = (arrive, pick, v)
        arrive, pick, v = best
        if arrive > h:
            dropped.append((a, b, q, f"would finish at {arrive:.0f} min, after the shift"))
            continue
        van_count[v] += 1
        moves.append(Move(v + 1, van_count[v], a, b, q, pick, arrive, deadline))
        van_free[v] = arrive
        van_at[v] = (sb.lat, sb.lon)
        taken[a] += q
        given[b] += q

    moves.sort(key=lambda m: (m.van, m.order))
    return Plan(moves=moves, dropped=dropped, solve_seconds=0.0, status="")


def verify(inst: Instance, plan: Plan) -> list[str]:
    """Independent re-check of every limit. Returns a list of violations (empty = OK)."""
    p = inst.params
    by_id = inst.by_id()
    lat0, lon0 = inst.depot
    problems = []
    taken: dict[str, int] = {}
    given: dict[str, int] = {}
    vans: dict[int, list[Move]] = {}
    for m in plan.moves:
        vans.setdefault(m.van, []).append(m)
        if not 0 < m.bikes <= p.van_capacity:
            problems.append(f"van {m.van} trip {m.order}: carries {m.bikes} bikes")
        if m.from_id == m.to_id:
            problems.append(f"van {m.van} trip {m.order}: same pickup and drop-off")
        taken[m.from_id] = taken.get(m.from_id, 0) + m.bikes
        given[m.to_id] = given.get(m.to_id, 0) + m.bikes
        if m.arrive_at_min > p.horizon_min + 1e-6:
            problems.append(f"van {m.van} trip {m.order}: finishes after the shift")
    for sid, n in taken.items():
        if n > by_id[sid].bikes:
            problems.append(f"{sid}: takes {n} bikes but only {by_id[sid].bikes} there")
    for sid, n in given.items():
        if n > by_id[sid].docks:
            problems.append(f"{sid}: drops {n} bikes but only {by_id[sid].docks} free docks")
    for v, ms in vans.items():
        t, at = 0.0, inst.depot
        for m in sorted(ms, key=lambda m: m.order):
            a, b = by_id[m.from_id], by_id[m.to_id]
            t += drive_minutes(at, (a.lat, a.lon), lat0, lon0) + handling_minutes(m.bikes)
            t += drive_minutes((a.lat, a.lon), (b.lat, b.lon), lat0, lon0)
            t += handling_minutes(m.bikes)
            if t > m.arrive_at_min + 1e-6:
                problems.append(f"van {v} trip {m.order}: arrival time is too optimistic")
            t, at = max(t, m.arrive_at_min), (b.lat, b.lon)
    return problems


def plan(inst: Instance) -> Plan:
    started = time.perf_counter()
    transfers, status = choose_transfers(inst)
    result = schedule(inst, transfers)
    result.status = status
    result.solve_seconds = time.perf_counter() - started
    problems = verify(inst, result)
    if problems:
        raise AssertionError("planner produced an invalid plan: " + "; ".join(problems))
    return result


def simulate(inst: Instance, moves: list[Move], step_min: float = 1.0) -> dict[str, float]:
    """Minute-by-minute fluid model: riders arrive and leave at a steady rate.

    Returns riders who'd find no bike (`missed_pickups`) or no free dock
    (`missed_returns`). Pickups happen when loading finishes, drop-offs when
    unloading finishes; both are limited by what's physically there at that moment.
    """
    h = inst.params.horizon_min
    stock = {s.id: float(s.bikes) for s in inst.stations}
    cap = {s.id: float(s.capacity) for s in inst.stations}
    events = sorted(
        [(m.pickup_at_min, m.from_id, -m.bikes) for m in moves]
        + [(m.arrive_at_min, m.to_id, m.bikes) for m in moves]
    )
    missed_pickups = missed_returns = 0.0
    t, k = 0.0, 0
    while t < h - 1e-9:
        dt = min(step_min, h - t)
        while k < len(events) and events[k][0] <= t + 1e-9:
            _, sid, delta = events[k]
            stock[sid] = min(cap[sid], max(0.0, stock[sid] + delta))
            k += 1
        for s in inst.stations:
            want_out = s.departures / h * dt
            want_in = s.arrivals / h * dt
            got_out = min(want_out, stock[s.id] + want_in)
            missed_pickups += want_out - got_out
            level = stock[s.id] + want_in - got_out
            missed_returns += max(0.0, level - cap[s.id])
            stock[s.id] = min(cap[s.id], level)
        t += dt
    return {"missed_pickups": missed_pickups, "missed_returns": missed_returns}


def score(inst: Instance, result: Plan) -> dict[str, float]:
    """Riders who'd find no bike or no dock, with and without the plan."""
    before = simulate(inst, [])
    after = simulate(inst, result.moves)
    return {
        "riders_affected_no_moves": before["missed_pickups"] + before["missed_returns"],
        "riders_affected_with_plan": after["missed_pickups"] + after["missed_returns"],
        "no_bike_no_moves": before["missed_pickups"],
        "no_bike_with_plan": after["missed_pickups"],
        "no_dock_no_moves": before["missed_returns"],
        "no_dock_with_plan": after["missed_returns"],
        "bikes_moved": sum(m.bikes for m in result.moves),
        "trips": len(result.moves),
    }
