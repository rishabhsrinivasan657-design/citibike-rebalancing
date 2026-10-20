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
    predicted_rates,
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
    plans: dict[str, Plan],
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
        f"{len(rep.stations)} stations. Our plans were made from the {rep.start_local:%H:%M} "
        "bike counts plus either 12-month averages or the Step 4 predictions (forecast weather "
        "only); they never saw what happened next. Hidden demand in the replay always uses "
        "the averages, so the predictions don't grade themselves.",
        "",
        "| Version | No bike | No free dock | Riders affected | vs no vans "
        "| Bikes dropped off / picked up |",
        "|---|---|---|---|---|---|",
    ]
    moved = {
        "What actually happened (Lyft's vans)": f"{lyft_moves[0]} / {lyft_moves[1]}",
        "No vans at all": "0 / 0",
    }
    for name, p in plans.items():
        n = sum(m.bikes for m in p.moves)
        moved[name] = f"{n} / {n}"
    for name, o in outcomes.items():
        change = f"{(base - o.riders_affected) / base:.0%} fewer" if base else "–"
        if name == "No vans at all":
            change = "–"
        lines.append(
            f"| {name} | {o.missed_pickups:.0f} | {o.missed_returns:.0f} | "
            f"**{o.riders_affected:.0f}** | {change} | {moved[name]} |"
        )
    shortfall = max(outcomes[name].van_shortfall for name in plans) if plans else 0
    lines += [
        "",
        "**Checks**",
        f"- Replaying what actually happened (without hidden demand) reproduces the recorded "
        f"bike counts: largest difference **{replay_error:.0f} bikes** (must be 0).",
        f"- Recorder gaps in this window: {rep.recorder_gaps} minutes.",
        f"- Planned bikes our vans couldn't load or unload when they got there: "
        f"{shortfall:.0f} at most.",
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


def resolve_paths(args) -> dict[str, Path]:
    return {
        "minute": args.minute_table or hub_file("marts/fct_station_minute.parquet"),
        "station": args.station_table or hub_file("marts/dim_station.parquet"),
        "visits": args.visits_table or hub_file("marts/fct_inferred_rebalancing.parquet"),
        "profile": args.profile or hub_file("raw/profiles/station_hour.parquet"),
    }


def run_one(
    start: datetime,
    zone: str,
    vans: int,
    minutes: int,
    paths: dict[str, Path],
    demand: str,
    profile_rows: list[dict],
):
    """Plan at `start` from recorded counts, then replay the window three-plus ways."""
    day_type = day_type_of(start)
    info, status = recorded_snapshot(paths["minute"], paths["station"], start)
    params = Params(vans=vans, horizon_min=float(minutes))
    plans: dict[str, Plan] = {}
    inst = build_instance(profile_rows, info, status, zone, start, day_type, params)
    if demand in ("averages", "both"):
        plans["Our plan (planned with averages)"] = make_plan(inst)
    if demand in ("model", "both"):
        from rebalancer.forecast.predict import ModelNotApproved

        try:
            rates = predicted_rates(info, start, float(minutes))
        except ModelNotApproved:
            rates = None
        if rates is not None:
            inst = build_instance(profile_rows, info, status, zone, start, day_type, params, rates)
            plans["Our plan (planned with predictions)"] = make_plan(inst)

    by_id = {i.station_id: i for i in info}
    ids = [s.id for s in inst.stations]
    rep = load(
        paths["minute"],
        paths["visits"],
        paths["profile"],
        ids,
        {s: by_id[s].short_name or "" for s in ids},
        {s: by_id[s].name for s in ids},
        start,
        minutes,
        day_type,
    )
    outcomes = {
        "What actually happened (Lyft's vans)": simulate(rep, rep.van),
        "No vans at all": simulate(rep, None),
    }
    for name, p in plans.items():
        outcomes[name] = simulate(rep, moves_from_plan(rep, p.moves))
    lyft_moves = (
        sum(v for s in rep.stations for v in rep.van[s] if v > 0),
        -sum(v for s in rep.stations for v in rep.van[s] if v < 0),
    )
    return rep, outcomes, plans, lyft_moves


def recorded_mornings(minute_path: Path, at_time: str, minutes: int) -> list[datetime]:
    """Weekday mornings where the recorder was up for every minute of the window."""
    import duckdb

    hh, mm = map(int, at_time.split(":"))
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    rows = con.execute(
        f"""
        WITH m AS (
            SELECT DISTINCT minute, recorder_up,
                   timezone('America/New_York', minute) AS local
            FROM read_parquet('{minute_path.as_posix()}')
        )
        SELECT local::DATE AS d, count(*) FILTER (WHERE recorder_up) AS up
        FROM m
        WHERE hour(local) * 60 + minute(local) BETWEEN {hh * 60 + mm} AND {hh * 60 + mm + minutes}
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    out = []
    for d, up in rows:
        day = datetime(d.year, d.month, d.day, hh, mm)
        if up == minutes + 1 and day_type_of(day) == "weekday":
            out.append(day)
    return out


def render_all(rows: list[dict], zone: str, at_time: str) -> str:
    keys = [
        "What actually happened (Lyft's vans)",
        "No vans at all",
        "Our plan (planned with averages)",
        "Our plan (planned with predictions)",
    ]
    lines = [
        f"## Verification across recorded mornings: {zone.title()}, {at_time} (+60 min)",
        "",
        f"Weekday mornings with no recorder gaps: **{len(rows)}**. Riders who'd find no bike or "
        "no free dock (estimate), per morning:",
        "",
        "| Morning | Lyft (actual) | No vans | Our plan (averages) | Our plan (predictions) |",
        "|---|---|---|---|---|",
    ]
    totals = dict.fromkeys(keys, 0.0)
    for r in rows:
        cells = []
        for k in keys:
            v = r.get(k)
            cells.append("–" if v is None else f"{v:.0f}")
            totals[k] += v or 0.0
        lines.append(f"| {r['date']:%a %b %d} | " + " | ".join(cells) + " |")

    def pct(k: str) -> str:
        # Compare only over mornings where this version exists.
        have = [r for r in rows if r.get(k) is not None]
        base = sum(r["No vans at all"] for r in have)
        mine = sum(r[k] for r in have)
        return f"{(base - mine) / base:.0%} fewer" if base else "–"

    lines += [
        "| **Total** | " + " | ".join(f"**{totals[k]:.0f}**" for k in keys) + " |",
        "| vs no vans (same mornings) | "
        + " | ".join("–" if k == "No vans at all" else pct(k) for k in keys)
        + " |",
        "",
        "Every replay reproduces the recorded bike counts exactly before any what-if is "
        "applied. Assumptions are listed in docs/PLAN.md, Step 6.",
    ]
    if len(rows) < 10:
        lines += [
            "",
            f"_Only {len(rows)} morning(s) so far: treat as early evidence, "
            "not a verdict (target: 10+)._",
        ]
    return "\n".join(lines)


def main_all() -> None:
    parser = argparse.ArgumentParser(description="Verify every recorded weekday morning.")
    parser.add_argument("--zone", default="midtown", choices=sorted(ZONES))
    parser.add_argument("--at-time", default="08:00")
    parser.add_argument("--vans", type=int, default=3)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--out", type=Path, help="also write the table to this file")
    for name in ("minute-table", "station-table", "visits-table", "profile"):
        parser.add_argument(f"--{name}", type=Path)
    args = parser.parse_args()

    paths = resolve_paths(args)
    profile_rows = load_profile(paths["profile"])
    rows = []
    for day in recorded_mornings(paths["minute"], args.at_time, args.minutes):
        rep, outcomes, _, _ = run_one(
            day, args.zone, args.vans, args.minutes, paths, "both", profile_rows
        )
        if max_replay_error(rep) != 0:
            raise SystemExit(f"{day:%Y-%m-%d}: replay doesn't match the recording")
        rows.append({"date": day, **{k: o.riders_affected for k, o in outcomes.items()}})
    report = render_all(rows, args.zone, args.at_time)
    print(report)
    if args.out:
        args.out.write_text(report + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")


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
    parser.add_argument(
        "--demand",
        choices=["model", "averages", "both"],
        default="both",
        help="what our planner expects: Step 4 predictions, averages, or both",
    )
    args = parser.parse_args()

    start = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    paths = resolve_paths(args)
    rep, outcomes, plans, lyft_moves = run_one(
        start,
        args.zone,
        args.vans,
        args.minutes,
        paths,
        args.demand,
        load_profile(paths["profile"]),
    )
    report = render(rep, outcomes, plans, lyft_moves, max_replay_error(rep), args.zone)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
