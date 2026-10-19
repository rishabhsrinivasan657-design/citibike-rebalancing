"""Plan van moves for a real zone and time, and print a readable move list.

    plan-moves --start 08:00 --vans 3                      # Midtown, weekday 8–9am
    plan-moves --profile raw/profiles/station_hour.parquet # local profile file

Bike counts come from the live feed at run time; expected departures and arrivals
come from the station-hour profile (Step 2) for the chosen day type and time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from rebalancer.config import Settings
from rebalancer.ingest.gbfs import (
    StationInformation,
    StationStatus,
    discover_feeds,
    fetch_json,
    parse_feed,
)
from rebalancer.planner.geo import straight_distance_m
from rebalancer.planner.model import Instance, Params, Plan, Station, minutes_until_problem
from rebalancer.planner.schedule import plan, score

ZONES = {"midtown": ((40.7536, -73.9832), 1500.0)}  # centre (Bryant Park), radius in metres


def expected_counts(rates: dict[int, tuple[float, float]], start: datetime, minutes: float):
    """Sum hourly averages over [start, start + minutes), pro-rating partial hours."""
    dep = arr = 0.0
    t, end = start, start + timedelta(minutes=minutes)
    while t < end:
        next_hour = (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        chunk = (min(next_hour, end) - t).total_seconds() / 3600
        d, a = rates.get(t.hour, (0.0, 0.0))
        dep, arr = dep + d * chunk, arr + a * chunk
        t = min(next_hour, end)
    return dep, arr


def build_instance(
    profile_rows: list[dict],
    info: list[StationInformation],
    status: list[StationStatus],
    zone: str,
    start: datetime,
    day_type: str,
    params: Params,
) -> Instance:
    centre, radius = ZONES[zone]
    rates: dict[str, dict[int, tuple[float, float]]] = {}
    for r in profile_rows:
        if r["day_type"] == day_type:
            rates.setdefault(r["station_id"], {})[r["hour"]] = (r["departures"], r["arrivals"])
    live = {s.station_id: s for s in status}
    stations = []
    for s in info:
        st = live.get(s.station_id)
        if st is None or not (st.is_installed and st.is_renting and st.is_returning):
            continue
        if straight_distance_m((s.lat, s.lon), centre, *centre) > radius:
            continue
        dep, arr = expected_counts(rates.get(s.short_name or "", {}), start, params.horizon_min)
        stations.append(
            Station(
                s.station_id,
                s.name,
                s.lat,
                s.lon,
                bikes=st.num_bikes_available,
                docks=st.num_docks_available,
                departures=dep,
                arrivals=arr,
            )
        )
    label = f"{zone.title()}, {day_type} {start:%H:%M}–" + (
        f"{start + timedelta(minutes=params.horizon_min):%H:%M}"
    )
    return Instance(stations, centre, params, label)


def render(inst: Instance, result: Plan, start: datetime) -> str:
    s = score(inst, result)
    names = {st.id: st.name for st in inst.stations}
    h = inst.params.horizon_min

    def clock(m: float) -> str:
        return f"{start + timedelta(minutes=m):%H:%M}"

    def pct(before: float, after: float) -> str:
        return f"{(before - after) / before:.0%} fewer" if before else "–"

    lines = [
        f"## Van plan: {inst.label}",
        "",
        f"{len(inst.stations)} stations, {inst.params.vans} vans of "
        f"{inst.params.van_capacity} bikes, solved in {result.solve_seconds:.1f} s "
        f"({result.status}).",
        "",
        "| Riders who'd find… | No moves | With plan | Change |",
        "|---|---|---|---|",
        f"| no bike | {s['no_bike_no_moves']:.0f} | {s['no_bike_with_plan']:.0f} | "
        f"{pct(s['no_bike_no_moves'], s['no_bike_with_plan'])} |",
        f"| no free dock | {s['no_dock_no_moves']:.0f} | {s['no_dock_with_plan']:.0f} | "
        f"{pct(s['no_dock_no_moves'], s['no_dock_with_plan'])} |",
        f"| **either** | **{s['riders_affected_no_moves']:.0f}** | "
        f"**{s['riders_affected_with_plan']:.0f}** | "
        f"**{pct(s['riders_affected_no_moves'], s['riders_affected_with_plan'])}** |",
        "",
        f"{s['trips']} trips, {s['bikes_moved']} bikes moved.",
        "",
    ]
    if result.moves:
        lines += [
            "| Van | # | Take from | Drop at | Bikes | Loaded by | Delivered by | Runs out at |",
            "|---|---|---|---|---|---|---|---|",
        ]
        by_id = inst.by_id()
        for m in result.moves:
            t_out = minutes_until_problem(by_id[m.to_id], h)
            runs_out = clock(t_out) if t_out < h else "after the hour"
            lines.append(
                f"| {m.van} | {m.order} | {names[m.from_id]} | {names[m.to_id]} | {m.bikes} | "
                f"{clock(m.pickup_at_min)} | {clock(m.arrive_at_min)} | {runs_out} |"
            )
    if result.dropped:
        lines += ["", f"{len(result.dropped)} chosen trips were dropped as not doable in time."]
    lines += [
        "",
        "_Bike counts are from the live feed when this ran; expected riders are averages for "
        "this day type and time over the last 12 months. Riders who'd find no bike are an "
        "estimate from a steady-rate simulation._",
    ]
    return "\n".join(lines)


def load_profile(path: Path | None) -> list[dict]:
    if path is None:
        from huggingface_hub import HfApi, hf_hub_download

        from rebalancer.ingest.recorder import resolve_repo_id

        token = os.environ.get("HF_TOKEN")
        if not token:
            sys.exit("Set HF_TOKEN, or pass --profile with a local file")
        repo = resolve_repo_id(
            HfApi(token=token),
            os.environ.get("HF_HISTORY_REPO", "citibike-history"),
            need_write=False,
        )
        path = Path(
            hf_hub_download(
                repo, "raw/profiles/station_hour.parquet", repo_type="dataset", token=token
            )
        )
    return pq.read_table(path).to_pylist()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", default="midtown", choices=sorted(ZONES))
    parser.add_argument("--start", default="08:00", help="local time HH:MM")
    parser.add_argument("--day-type", default="weekday", choices=["weekday", "weekend"])
    parser.add_argument("--vans", type=int, default=3)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = Settings()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        feeds = discover_feeds(client, s.gbfs_discovery_url, s.gbfs_language)
        info = fetch_json(client, feeds["station_information"], 3)
        status = fetch_json(client, feeds["station_status"], 3)
    if not (info.ok and status.ok):
        sys.exit("could not fetch the live station feeds")

    hh, mm = map(int, args.start.split(":"))
    start = datetime(2000, 1, 3, hh, mm)  # only the time of day is used
    params = Params(vans=args.vans, horizon_min=args.minutes)
    inst = build_instance(
        load_profile(args.profile),
        parse_feed(info.payload, StationInformation).stations,
        parse_feed(status.payload, StationStatus).stations,
        args.zone,
        start,
        args.day_type,
        params,
    )
    report = render(inst, plan(inst), start)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
