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


def _pack_trips(inst: Instance, transfers: list[Transfer]) -> list[tuple[float, str, list]]:
    """Group transfers by donor into trips of at most `van_capacity` bikes and
    `max_stops` drop-offs, most urgent stops first.

    Returns (earliest deadline, donor, [(deadline, receiver, bikes), ...]) per trip,
    most urgent trip first.
    """
    p = inst.params
    h = p.horizon_min
    by_id = inst.by_id()
    by_donor: dict[str, list[tuple[float, str, int]]] = {}
    for t in transfers:
        deadline = min(h, minutes_until_problem(by_id[t.to_id], h))
        by_donor.setdefault(t.from_id, []).append((deadline, t.to_id, t.bikes))

    trips: list[tuple[float, str, list]] = []
    for donor, drops in by_donor.items():
        current: list[tuple[float, str, int]] = []
        load = 0
        for deadline, to_id, bikes in sorted(drops):
            while bikes > 0:
                if load == p.van_capacity or len(current) == p.max_stops:
                    trips.append((current[0][0], donor, current))
                    current, load = [], 0
                q = min(bikes, p.van_capacity - load)
                current.append((deadline, to_id, q))
                load += q
                bikes -= q
        if current:
            trips.append((current[0][0], donor, current))
    return sorted(trips, key=lambda t: (t[0], t[1]))


def schedule(inst: Instance, transfers: list[Transfer]) -> Plan:
    p = inst.params
    h = p.horizon_min
    lat0, lon0 = inst.depot
    by_id = inst.by_id()

    def loc(sid: str) -> tuple[float, float]:
        return (by_id[sid].lat, by_id[sid].lon)

    van_free = [0.0] * p.vans
    van_at = [inst.depot] * p.vans
    van_legs = [0] * p.vans
    van_trips = [0] * p.vans
    taken = dict.fromkeys(by_id, 0)  # bikes removed from each station so far
    given = dict.fromkeys(by_id, 0)  # bikes delivered to each station so far
    moves: list[Move] = []
    dropped: list[tuple[str, str, int, str]] = []

    for _, donor, drops in _pack_trips(inst, transfers):
        # Limit each drop to what's physically left at the donor and the receiver.
        legs = []
        left = by_id[donor].bikes - taken[donor]
        for deadline, to_id, q in drops:
            q = min(q, left, by_id[to_id].docks - given[to_id])
            if q <= 0:
                dropped.append((donor, to_id, 0, "no bikes or docks left"))
                continue
            legs.append((deadline, to_id, q))
            left -= q
        if not legs:
            continue

        def route(v: int, legs: list, donor: str = donor) -> tuple[float, float, list[float]]:
            load = sum(q for _, _, q in legs)
            pick = van_free[v] + drive_minutes(van_at[v], loc(donor), lat0, lon0)
            pick += handling_minutes(load)
            t, at, arrivals = pick, loc(donor), []
            for _, to_id, q in legs:
                t += drive_minutes(at, loc(to_id), lat0, lon0) + handling_minutes(q)
                arrivals.append(t)
                at = loc(to_id)
            return t, pick, arrivals

        best = min(range(p.vans), key=lambda v: route(v, legs)[0])
        finish, pick, arrivals = route(best, legs)
        # If the whole trip doesn't fit in the shift, drop the least urgent stops.
        while legs and finish > h:
            _, to_id, q = legs.pop()
            dropped.append((donor, to_id, q, "would finish after the shift"))
            if legs:
                best = min(range(p.vans), key=lambda v: route(v, legs)[0])
                finish, pick, arrivals = route(best, legs)
        if not legs:
            continue

        van_trips[best] += 1
        for (deadline, to_id, q), arrive in zip(legs, arrivals, strict=True):
            van_legs[best] += 1
            moves.append(
                Move(
                    best + 1,
                    van_legs[best],
                    donor,
                    to_id,
                    q,
                    pick,
                    arrive,
                    deadline,
                    trip=van_trips[best],
                )
            )
            given[to_id] += q
            taken[donor] += q
        van_free[best] = finish
        van_at[best] = loc(legs[-1][1])

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
    trips: dict[tuple[int, int], list[Move]] = {}
    for m in plan.moves:
        trips.setdefault((m.van, m.trip if m.trip is not None else -m.order), []).append(m)
        if m.bikes <= 0:
            problems.append(f"van {m.van} leg {m.order}: carries {m.bikes} bikes")
        if m.from_id == m.to_id:
            problems.append(f"van {m.van} leg {m.order}: same pickup and drop-off")
        taken[m.from_id] = taken.get(m.from_id, 0) + m.bikes
        given[m.to_id] = given.get(m.to_id, 0) + m.bikes
        if m.arrive_at_min > p.horizon_min + 1e-6:
            problems.append(f"van {m.van} leg {m.order}: finishes after the shift")
    for sid, n in taken.items():
        if n > by_id[sid].bikes:
            problems.append(f"{sid}: takes {n} bikes but only {by_id[sid].bikes} there")
    for sid, n in given.items():
        if n > by_id[sid].docks:
            problems.append(f"{sid}: drops {n} bikes but only {by_id[sid].docks} free docks")

    vans: dict[int, list[list[Move]]] = {}
    for (v, _), legs in trips.items():
        legs.sort(key=lambda m: m.order)
        load = sum(m.bikes for m in legs)
        k = legs[0].trip if legs[0].trip is not None else legs[0].order
        if load > p.van_capacity:
            problems.append(f"van {v} trip {k}: carries {load} bikes")
        if len(legs) > p.max_stops:
            problems.append(f"van {v} trip {k}: {len(legs)} stops")
        if len({m.from_id for m in legs}) > 1:
            problems.append(f"van {v} trip {k}: more than one pickup station")
        vans.setdefault(v, []).append(legs)
    for v, van_trips in vans.items():
        t, at = 0.0, inst.depot
        for legs in sorted(van_trips, key=lambda legs: legs[0].order):
            a = by_id[legs[0].from_id]
            t += drive_minutes(at, (a.lat, a.lon), lat0, lon0)
            t += handling_minutes(sum(m.bikes for m in legs))
            at = (a.lat, a.lon)
            for m in legs:
                b = by_id[m.to_id]
                t += drive_minutes(at, (b.lat, b.lon), lat0, lon0) + handling_minutes(m.bikes)
                if t > m.arrive_at_min + 1e-6:
                    problems.append(f"van {v} leg {m.order}: arrival time is too optimistic")
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
        "trips": len({(m.van, m.trip if m.trip is not None else -m.order) for m in result.moves}),
        "stops": len(result.moves),
    }
