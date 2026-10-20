"""Logic behind the Streamlit app, kept separate from the page so it can be tested.

`live_plan()` fetches live bike counts, predicts the next hour (or falls back to
averages), plans van moves for a zone, and returns everything the page shows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from rebalancer.config import Settings
from rebalancer.ingest.gbfs import (
    StationInformation,
    StationStatus,
    discover_feeds,
    fetch_json,
    parse_feed,
)
from rebalancer.planner.model import Instance, Params, Plan, minutes_until_problem
from rebalancer.planner.run import build_instance, hub_file
from rebalancer.planner.schedule import plan as make_plan
from rebalancer.planner.schedule import score

NY = ZoneInfo("America/New_York")

GREEN, ORANGE, RED, GREY = [46, 160, 67], [230, 140, 20], [215, 48, 39], [150, 150, 150]


@dataclass
class LivePlan:
    now_local: datetime
    inst: Instance
    plan: Plan
    score: dict[str, float]
    demand_source: str
    stations: list[dict] = field(default_factory=list)  # one row per station, for the map
    arcs: list[dict] = field(default_factory=list)  # one row per van stop, for the map
    moves: list[dict] = field(default_factory=list)  # the move list table


def fetch_live() -> tuple[list[StationInformation], list[StationStatus]]:
    s = Settings()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        feeds = discover_feeds(client, s.gbfs_discovery_url, s.gbfs_language)
        info = fetch_json(client, feeds["station_information"], 3)
        status = fetch_json(client, feeds["station_status"], 3)
    if not (info.ok and status.ok):
        raise RuntimeError("Citi Bike's live feed didn't respond; try again in a minute.")
    return (
        parse_feed(info.payload, StationInformation).stations,
        parse_feed(status.payload, StationStatus).stations,
    )


def day_type_of(t: datetime) -> str:
    import holidays

    hol = t.date() in holidays.country_holidays("US", subdiv="NY", years=t.year)
    return "weekend" if t.weekday() >= 5 or hol else "weekday"


def live_plan(
    zone: str,
    vans: int,
    minutes: int,
    use_model: bool,
    profile_rows: list[dict],
    live: tuple[list[StationInformation], list[StationStatus]],
    now_local: datetime,
    rates_fn=None,
) -> LivePlan:
    info, status = live
    start = now_local.replace(second=0, microsecond=0, tzinfo=None)
    day_type = day_type_of(start)
    params = Params(vans=vans, horizon_min=float(minutes))

    rates, source = None, "12-month averages"
    if use_model:
        from rebalancer.forecast.predict import ModelNotApproved

        try:
            from rebalancer.planner import run as planner_run

            rates = (rates_fn or planner_run.predicted_rates)(info, start, float(minutes))
            source = "Step 4 predictions (live weather forecast)"
        except ModelNotApproved:
            source = "12-month averages (the prediction model isn't approved)"

    inst = build_instance(profile_rows, info, status, zone, start, day_type, params, rates)
    result = make_plan(inst)
    out = LivePlan(now_local, inst, result, score(inst, result), source)

    h = params.horizon_min
    for s in inst.stations:
        t = minutes_until_problem(s, h)
        running_out = s.departures > s.arrivals
        if t >= h:
            colour, label = GREEN, "fine for the hour"
        elif running_out:
            colour, label = RED, f"runs out of bikes in ~{t:.0f} min"
        else:
            colour, label = ORANGE, f"fills up in ~{t:.0f} min"
        if s.bikes == 0 and running_out:
            label = "empty now"
        if s.docks == 0 and not running_out:
            label = "full now"
        out.stations.append(
            {
                "name": s.name,
                "lat": s.lat,
                "lon": s.lon,
                "bikes": s.bikes,
                "docks": s.docks,
                "expected_out": round(s.departures, 1),
                "expected_in": round(s.arrivals, 1),
                "status": label,
                "colour": colour if (s.bikes + s.docks) else GREY,
                "tip": f"{s.name}: {label}. {s.bikes} bikes, {s.docks} free docks. Expected next "
                f"{h:.0f} min: {s.departures:.0f} out, {s.arrivals:.0f} in.",
            }
        )

    by_id = inst.by_id()
    for m in result.moves:
        a, b = by_id[m.from_id], by_id[m.to_id]
        out.arcs.append(
            {
                "from_lat": a.lat,
                "from_lon": a.lon,
                "to_lat": b.lat,
                "to_lon": b.lon,
                "tip": f"Van {m.van}, trip {m.trip or m.order}: {m.bikes} bikes "
                f"{a.name} → {b.name}",
                "width": max(2, m.bikes / 3),
            }
        )
        out.moves.append(
            {
                "Van": m.van,
                "Trip": m.trip or m.order,
                "Take from": a.name,
                "Drop at": b.name,
                "Bikes": m.bikes,
                "Loaded by": f"{start + timedelta(minutes=m.pickup_at_min):%H:%M}",
                "Delivered by": f"{start + timedelta(minutes=m.arrive_at_min):%H:%M}",
            }
        )
    return out


def model_card() -> dict | None:
    """The demand model's test results (models/metrics.json), if available."""
    try:
        return json.loads(Path(hub_file("models/metrics.json")).read_text())
    except Exception:  # missing file or no token: the page just hides the card
        return None
