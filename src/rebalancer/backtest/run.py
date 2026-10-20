"""Backtest one recorded morning: what happened vs no vans vs our plan.

backtest --at "2026-10-20 08:00"            # downloads tables from Hugging Face
backtest --at "..." --minute-table ... --station-table ... --visits-table ... --profile ...
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

import holidays

from rebalancer.backtest.replay import (
    Outcome,
    Replay,
    load,
    max_replay_error,
    moves_from_plan,
    simulate,
)
from rebalancer.planner.model import Params, Plan
from rebalancer.planner.run import (
    ZONES,
    build_instance,
    hub_file,
    load_profile,
    recorded_snapshot,
)
from rebalancer.planner.schedule import plan as make_plan


def day_type_of(day: datetime) -> str:
    weekend = day.weekday() >= 5 or day.date() in holidays.country_holidays(
        "US", subdiv="NY", years=day.year
    )
    return "weekend" if weekend else "weekday"


def render(
    rep: Replay,
    outcomes: dict[str, Outcome],
    our_plan: Plan,
    lyft_moves: tuple[int, int],
    replay_error: float,
    zone: str,
) -> str:
    end = rep.start_local + timedelta(minutes=rep.minutes)
    base = outcomes["No vans at all"].riders_affected
    lines = [
        f"## Backtest: {zone.title()}, {rep.start_local:%a %b %d}, "
        f"{rep.start_local:%H:%M}–{end:%H:%M} (New York time)",
        "",
        f"{len(rep.stations)} stations. Our plan was made from the {rep.start_local:%H:%M} "
        "bike counts and 12-month averages only; it never saw what happened next.",
        "",
        "| Version | No bike | No free dock | Riders affected | vs no vans "
        "| Bikes dropped off / picked up |",
        "|---|---|---|---|---|---|",
    ]
    ours_moved = sum(m.bikes for m in our_plan.moves)
    moved = {
        "What actually happened (Lyft's vans)": f"{lyft_moves[0]} / {lyft_moves[1]}",
        "No vans at all": "0 / 0",
        "Our plan instead of Lyft's": f"{ours_moved} / {ours_moved}",
    }
    for name, o in outcomes.items():
        change = f"{(base - o.riders_affected) / base:.0%} fewer" if base else "–"
        if name == "No vans at all":
            change = "–"
        lines.append(
            f"| {name} | {o.missed_pickups:.0f} | {o.missed_returns:.0f} | "
            f"**{o.riders_affected:.0f}** | {change} | {moved[name]} |"
        )
    ours = outcomes["Our plan instead of Lyft's"]
    lines += [
        "",
        "**Checks**",
        f"- Replaying what actually happened (without hidden demand) reproduces the recorded "
        f"bike counts: largest difference **{replay_error:.0f} bikes** (must be 0).",
        f"- Recorder gaps in this window: {rep.recorder_gaps} minutes.",
        f"- Planned bikes our vans couldn't load or unload when they got there: "
        f"{ours.van_shortfall:.0f}.",
        "",
        "**Assumptions** (read before trusting the numbers)",
        "- Rider activity is the recorded change in bikes minus Lyft's detected van visits.",
        "- While a station was really empty (or full), riders who wanted a bike (or a dock) "
        "left no trace; we add that station's usual number for that time. These are 12-month "
        "averages, not that morning's actual demand.",
        "- A rider turned away is counted as missed; in reality some walk to a neighbouring "
        "station, so all versions overstate misses, and rebalancing gets some extra credit.",
        "- One morning is an anecdote. The verdict needs 10+ weekday mornings.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", required=True, help="New York time 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--zone", default="midtown", choices=sorted(ZONES))
    parser.add_argument("--vans", type=int, default=3)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--minute-table", type=Path)
    parser.add_argument("--station-table", type=Path)
    parser.add_argument("--visits-table", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()

    start = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    day_type = day_type_of(start)
    minute_path = args.minute_table or hub_file("marts/fct_station_minute.parquet")
    station_path = args.station_table or hub_file("marts/dim_station.parquet")
    visits_path = args.visits_table or hub_file("marts/fct_inferred_rebalancing.parquet")
    profile_path = args.profile or hub_file("raw/profiles/station_hour.parquet")

    info, status = recorded_snapshot(minute_path, station_path, start)
    params = Params(vans=args.vans, horizon_min=float(args.minutes))
    inst = build_instance(
        load_profile(profile_path), info, status, args.zone, start, day_type, params
    )
    our_plan = make_plan(inst)

    by_id = {i.station_id: i for i in info}
    ids = [s.id for s in inst.stations]
    rep = load(
        minute_path,
        visits_path,
        profile_path,
        ids,
        {s: by_id[s].short_name or "" for s in ids},
        {s: by_id[s].name for s in ids},
        start,
        args.minutes,
        day_type,
    )
    outcomes = {
        "What actually happened (Lyft's vans)": simulate(rep, rep.van),
        "No vans at all": simulate(rep, None),
        "Our plan instead of Lyft's": simulate(rep, moves_from_plan(rep, our_plan.moves)),
    }
    lyft_moves = (
        sum(v for s in rep.stations for v in rep.van[s] if v > 0),
        -sum(v for s in rep.stations for v in rep.van[s] if v < 0),
    )
    report = render(rep, outcomes, our_plan, lyft_moves, max_replay_error(rep), args.zone)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
