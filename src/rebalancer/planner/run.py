"""Plan van moves for a real zone and time, and print a readable move list.

    plan-moves --start 08:00 --vans 3                      # Midtown, weekday 8–9am
    plan-moves --profile raw/profiles/station_hour.parquet # local profile file
    plan-moves --at "2026-10-20 08:00"                     # a recorded morning

Bike counts come from the live feed at run time, or with --at from the recorded
minute table (Step 3) at that New York time. Expected departures and arrivals come
from the station-hour profile (Step 2) for the day type and time.
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


def render(
    inst: Instance, result: Plan, start: datetime, source: str = "from the live feed when this ran"
) -> str:
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
        f"{s['trips']} trips with {s['stops']} drop-off stops, {s['bikes_moved']} bikes moved.",
        "",
    ]
    if result.moves:
        lines += [
            "| Van | Trip | Take from | Drop at | Bikes | Loaded by | Delivered by | Runs out at |",
            "|---|---|---|---|---|---|---|---|",
        ]
        by_id = inst.by_id()
        for m in result.moves:
            t_out = minutes_until_problem(by_id[m.to_id], h)
            runs_out = clock(t_out) if t_out < h else "after the hour"
            lines.append(
                f"| {m.van} | {m.trip or m.order} | {names[m.from_id]} | {names[m.to_id]} "
                f"| {m.bikes} | {clock(m.pickup_at_min)} | {clock(m.arrive_at_min)} | {runs_out} |"
            )
    if result.dropped:
        lines += ["", f"{len(result.dropped)} chosen trips were dropped as not doable in time."]
    lines += [
        "",
        f"_Bike counts are {source}; expected riders are averages for "
        "this day type and time over the last 12 months. Riders who'd find no bike are an "
        "estimate from a steady-rate simulation._",
    ]
    return "\n".join(lines)


def hub_file(path_in_repo: str) -> Path:
    """Download one file from the history dataset (needs HF_TOKEN)."""
    from huggingface_hub import HfApi, hf_hub_download

    from rebalancer.ingest.recorder import resolve_repo_id

    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("Set HF_TOKEN, or pass local files")
    repo = resolve_repo_id(
        HfApi(token=token), os.environ.get("HF_HISTORY_REPO", "citibike-history"), False
    )
    return Path(hf_hub_download(repo, path_in_repo, repo_type="dataset", token=token))


def load_profile(path: Path | None) -> list[dict]:
    return pq.read_table(path or hub_file("raw/profiles/station_hour.parquet")).to_pylist()


def recorded_snapshot(
    minute_path: Path, station_path: Path, at_local: datetime
) -> tuple[list[StationInformation], list[StationStatus]]:
    """Station list and bike counts as recorded at a New York local minute."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    at = f"timezone('America/New_York', TIMESTAMP '{at_local:%Y-%m-%d %H:%M:00}')"
    status_rows = con.execute(
        f"""SELECT station_id, num_bikes_available, num_ebikes_available, num_bikes_disabled,
                   num_docks_available, num_docks_disabled, is_renting, is_returning,
                   epoch(last_reported)::BIGINT AS last_reported
            FROM read_parquet('{minute_path.as_posix()}')
            WHERE minute = {at} AND recorder_up"""
    ).fetchall()
    if not status_rows:
        sys.exit(f"no recorded bike counts at {at_local:%Y-%m-%d %H:%M} New York time")
    info_rows = con.execute(
        f"""SELECT station_id, short_name, name, lat, lon, region_id, capacity
            FROM read_parquet('{station_path.as_posix()}')
            WHERE valid_from <= {at} AND (valid_to IS NULL OR valid_to > {at})"""
    ).fetchall()
    info = [
        StationInformation(
            station_id=r[0],
            short_name=r[1],
            name=r[2],
            lat=r[3],
            lon=r[4],
            region_id=r[5],
            capacity=r[6],
        )
        for r in info_rows
    ]
    status = [
        StationStatus(
            station_id=r[0],
            num_bikes_available=r[1],
            num_ebikes_available=r[2],
            num_bikes_disabled=r[3],
            num_docks_available=r[4],
            num_docks_disabled=r[5],
            is_installed=True,
            is_renting=r[6],
            is_returning=r[7],
            last_reported=r[8],
        )
        for r in status_rows
    ]
    return info, status


def save_instance(inst: Instance, start: datetime, source: str, path: Path) -> None:
    import dataclasses
    import json

    data = {
        "label": inst.label,
        "start": start.isoformat(),
        "source": source,
        "depot": list(inst.depot),
        "params": dataclasses.asdict(inst.params),
        "stations": [dataclasses.asdict(s) for s in inst.stations],
    }
    path.write_text(json.dumps(data, indent=1))


def load_instance(path: Path) -> tuple[Instance, datetime, str]:
    import json

    data = json.loads(path.read_text())
    inst = Instance(
        [Station(**s) for s in data["stations"]],
        tuple(data["depot"]),
        Params(**data["params"]),
        data["label"],
    )
    return inst, datetime.fromisoformat(data["start"]), data["source"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", default="midtown", choices=sorted(ZONES))
    parser.add_argument("--start", default="08:00", help="local time HH:MM")
    parser.add_argument("--day-type", default="weekday", choices=["weekday", "weekend"])
    parser.add_argument("--vans", type=int, default=3)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--profile", type=Path)
    parser.add_argument(
        "--at",
        help="use recorded bike counts at this New York time, "
        "'YYYY-MM-DD HH:MM' (sets --start and --day-type)",
    )
    parser.add_argument("--minute-table", type=Path, help="local fct_station_minute.parquet")
    parser.add_argument("--station-table", type=Path, help="local dim_station.parquet")
    parser.add_argument("--save-instance", type=Path, help="write the planning problem as JSON")
    parser.add_argument("--instance", type=Path, help="plan a saved problem instead")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    params = Params(vans=args.vans, horizon_min=args.minutes)
    if args.instance:
        inst, start, source = load_instance(args.instance)
        print(render(inst, plan(inst), start, source))
        return
    if args.at:
        import holidays

        start = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        weekend = start.weekday() >= 5 or start.date() in holidays.country_holidays(
            "US", subdiv="NY", years=start.year
        )
        day_type = "weekend" if weekend else "weekday"
        info_list, status_list = recorded_snapshot(
            args.minute_table or hub_file("marts/fct_station_minute.parquet"),
            args.station_table or hub_file("marts/dim_station.parquet"),
            start,
        )
        source = f"recorded at {start:%a %b %d, %H:%M} New York time"
    else:
        s = Settings()
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            feeds = discover_feeds(client, s.gbfs_discovery_url, s.gbfs_language)
            info = fetch_json(client, feeds["station_information"], 3)
            status = fetch_json(client, feeds["station_status"], 3)
        if not (info.ok and status.ok):
            sys.exit("could not fetch the live station feeds")
        hh, mm = map(int, args.start.split(":"))
        start = datetime(2000, 1, 3, hh, mm)  # only the time of day is used
        day_type = args.day_type
        info_list = parse_feed(info.payload, StationInformation).stations
        status_list = parse_feed(status.payload, StationStatus).stations
        source = "from the live feed when this ran"

    inst = build_instance(
        load_profile(args.profile), info_list, status_list, args.zone, start, day_type, params
    )
    if args.save_instance:
        save_instance(inst, start, source, args.save_instance)
    report = render(inst, plan(inst), start, source)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
