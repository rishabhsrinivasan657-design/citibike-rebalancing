"""Glue for running the dbt project on GitHub (see .github/workflows/transform.yml).

warehouse pull --work ./work      # history raw/ + live lake/ from Hugging Face
warehouse push --work ./work      # finished marts/ to the history dataset
warehouse report --work ./work    # summary + van moves to check by hand
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb


def _api_and_repos():
    from huggingface_hub import HfApi

    from rebalancer.ingest.recorder import resolve_repo_id

    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN must be set")
    api = HfApi(token=token)
    history = resolve_repo_id(api, os.environ.get("HF_HISTORY_REPO", "citibike-history"), True)
    live = resolve_repo_id(api, os.environ.get("HF_REPO", "citibike-station-status"), False)
    return api, token, history, live


def pull(work: Path) -> None:
    from huggingface_hub import snapshot_download

    _, token, history, live = _api_and_repos()
    snapshot_download(
        history, repo_type="dataset", token=token, local_dir=work, allow_patterns=["raw/**"]
    )
    snapshot_download(
        live, repo_type="dataset", token=token, local_dir=work, allow_patterns=["lake/**"]
    )


def push(work: Path) -> None:
    api, _, history, _ = _api_and_repos()
    api.upload_folder(
        repo_id=history,
        repo_type="dataset",
        folder_path=str(work / "marts"),
        path_in_repo="marts",
        commit_message="Update marts",
        delete_patterns=["**"],
    )


def report(work: Path, sample: int = 10) -> str:
    m = (work / "marts").as_posix()
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")

    def one(sql: str):
        return con.execute(sql).fetchone()

    def t(name: str) -> str:
        return f"read_parquet('{m}/{name}')"

    trips = one(f"SELECT count(*), count(start_station_id) FROM {t('fct_trips/*/*.parquet')}")
    stations = one(
        "SELECT count(DISTINCT station_id), count(*), count(*) FILTER (WHERE NOT is_current) "
        f"FROM {t('dim_station.parquet')}"
    )
    minutes = one(
        "SELECT count(*), min(minute), max(minute), avg(is_empty::int), avg(is_full::int) "
        f"FROM {t('fct_station_minute.parquet')}"
    )
    rec = one(f"SELECT count(*), avg(recorder_up::int) FROM {t('fct_recorder_minute.parquet')}")
    vans = one(
        "SELECT count(*), count(*) FILTER (WHERE action = 'drop_off') "
        f"FROM {t('fct_inferred_rebalancing.parquet')}"
    )
    lines = [
        "## Warehouse build",
        "",
        "| Table | What's in it |",
        "|---|---|",
        f"| fct_trips | {trips[0]:,} rides; {trips[1] / trips[0]:.1%} matched to a live station |",
        f"| dim_station | {stations[0]:,} stations, {stations[1]:,} versions "
        f"({stations[2]:,} closed) |",
        f"| fct_station_minute | {minutes[0]:,} station-minutes, {minutes[1]:%Y-%m-%d %H:%M} → "
        f"{minutes[2]:%Y-%m-%d %H:%M} UTC; {minutes[3]:.1%} empty, {minutes[4]:.1%} full |",
        f"| fct_recorder_minute | {rec[0]:,} minutes; recorder up {rec[1]:.1%} |",
        f"| fct_inferred_rebalancing | {vans[0]:,} likely van visits ({vans[1]:,} drop-offs) |",
        "",
        f"### {sample} van visits to check by hand",
        "",
        "Bikes (available + disabled) per minute, from 3 minutes before to 2 after.",
        "",
    ]
    events = con.execute(
        f"""SELECT station_id, started_minute, ended_minute, bikes_change, action
            FROM read_parquet('{m}/fct_inferred_rebalancing.parquet')
            USING SAMPLE {sample} ROWS (reservoir, 42) ORDER BY started_minute"""
    ).fetchall()
    names = dict(
        con.execute(
            f"SELECT station_id, name FROM read_parquet('{m}/dim_station.parquet') WHERE is_current"
        ).fetchall()
    )
    for sid, start, end, change, action in events:
        series = con.execute(
            f"""SELECT strftime(minute, '%H:%M'), num_bikes_available + num_bikes_disabled,
                       recorder_up
                FROM read_parquet('{m}/fct_station_minute.parquet')
                WHERE station_id = ? AND minute BETWEEN ? - INTERVAL 3 MINUTE
                                                    AND ? + INTERVAL 2 MINUTE
                ORDER BY minute""",
            [sid, start, end],
        ).fetchall()
        trace = " → ".join(f"{t} {b}" + ("" if up else " (gap)") for t, b, up in series)
        lines.append(f"- **{names.get(sid, sid)}**, {action} {change:+d}: {trace}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pull", "push", "report"])
    parser.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args()
    if args.command == "pull":
        pull(args.work)
    elif args.command == "push":
        push(args.work)
    else:
        text = report(args.work)
        print(text)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as f:
                f.write(text + "\n")


if __name__ == "__main__":
    main()
