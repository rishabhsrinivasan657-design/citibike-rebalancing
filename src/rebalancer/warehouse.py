"""Glue for running the dbt project on GitHub (see .github/workflows/transform.yml).

warehouse pull --work ./work      # history raw/ + live lake/ from Hugging Face
warehouse push --work ./work      # finished marts/ to the history dataset
warehouse prune                   # free space taken by old copies of replaced files
warehouse report --work ./work    # summary + van moves to check by hand
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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


def stale_lfs_files(lfs_files: Iterable, keep: set[str], now: datetime, min_age: timedelta):
    """Stored files no current file points to. Recent uploads are skipped: another job
    may have uploaded them and not yet committed."""
    return [
        f
        for f in lfs_files
        if f.file_oid not in keep and (f.pushed_at is None or now - f.pushed_at > min_age)
    ]


def prune(min_age: timedelta = timedelta(hours=2)) -> None:
    """Hugging Face keeps every old copy of every file. Each transform rewrites ~3.4 GB
    of marts, so old copies filled the 100 GB private limit in a week (Oct 2026). The
    marts are rebuilt from raw/ each run, so old copies are never needed."""
    api, _, history, _ = _api_and_repos()
    keep = {
        f.lfs.sha256
        for f in api.list_repo_tree(history, repo_type="dataset", recursive=True)
        if getattr(f, "lfs", None)
    }
    stale = stale_lfs_files(
        api.list_lfs_files(history, repo_type="dataset"), keep, datetime.now(UTC), min_age
    )
    if stale:
        api.permanently_delete_lfs_files(history, stale, repo_type="dataset")
    print(f"pruned {len(stale)} old copies ({sum(f.size for f in stale) / 1e9:.1f} GB)")


def push(work: Path) -> None:
    prune()  # make room first
    api, _, history, _ = _api_and_repos()
    api.upload_folder(
        repo_id=history,
        repo_type="dataset",
        folder_path=str(work / "marts"),
        path_in_repo="marts",
        commit_message="Update marts",
        delete_patterns=["**"],
    )
    if (work / "okf").exists():
        api.upload_folder(
            repo_id=history,
            repo_type="dataset",
            folder_path=str(work / "okf"),
            path_in_repo="okf",
            commit_message="Update OKF bundle",
            delete_patterns=["**"],
        )
    prune()  # the copies this upload just replaced (all from earlier runs)


def pull_okf_log(work: Path) -> None:
    """Fetch the previous bundle's log.md so each build adds to the history."""
    from huggingface_hub import snapshot_download

    _, token, history, _ = _api_and_repos()
    snapshot_download(
        history,
        repo_type="dataset",
        token=token,
        local_dir=work / "okf_previous_dl",
        allow_patterns=["okf/log.md"],
    )
    src = work / "okf_previous_dl" / "okf" / "log.md"
    (work / "okf_previous").mkdir(parents=True, exist_ok=True)
    if src.exists():
        (work / "okf_previous" / "log.md").write_text(src.read_text())


def push_models(work: Path) -> None:
    api, _, history, _ = _api_and_repos()
    api.upload_folder(
        repo_id=history,
        repo_type="dataset",
        folder_path=str(work / "models"),
        path_in_repo="models",
        commit_message="Update demand model",
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
    odd = one(
        "SELECT count(*) FILTER (WHERE duration_min < 0), "
        "count(*) FILTER (WHERE duration_min < 0 AND started_at_local::date IN "
        "('2025-11-02', '2026-11-01') AND hour(started_at_local) IN (0, 1, 2)), "
        "count(*) FILTER (WHERE duration_min > 24 * 60) "
        f"FROM {t('fct_trips/*/*.parquet')}"
    )
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
        f"| fct_trips | {trips[0]:,} rides; {trips[1] / trips[0]:.1%} matched to a live station; "
        f"{odd[0]:,} end before they start ({odd[1]:,} of them on the night clocks go back); "
        f"{odd[2]:,} last over a day |",
        f"| dim_station | {stations[0]:,} stations, {stations[1]:,} versions "
        f"({stations[2]:,} closed) |",
        f"| fct_station_minute | {minutes[0]:,} station-minutes, {minutes[1]:%Y-%m-%d %H:%M} → "
        f"{minutes[2]:%Y-%m-%d %H:%M} UTC; {minutes[3]:.1%} empty, {minutes[4]:.1%} full |",
        f"| fct_recorder_minute | {rec[0]:,} minutes; recorder up {rec[1]:.1%} |",
        f"| fct_inferred_rebalancing | {vans[0]:,} likely van visits ({vans[1]:,} drop-offs) |",
        "",
        f"### {sample} van visits to check by hand",
        "",
        "Bikes (available + disabled) per minute, from 3 minutes before to 2 after, and how "
        "many bikes riders would usually take or return at that station in that time.",
        "",
    ]
    events = con.execute(
        f"""SELECT station_id, started_minute, ended_minute, bikes_change, minutes,
                   expected_riders
            FROM read_parquet('{m}/fct_inferred_rebalancing.parquet')
            USING SAMPLE {sample} ROWS (reservoir, 42) ORDER BY started_minute"""
    ).fetchall()
    names = dict(
        con.execute(
            f"SELECT station_id, name FROM read_parquet('{m}/dim_station.parquet') WHERE is_current"
        ).fetchall()
    )
    for sid, start, end, change, n_min, expected in events:
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
        lines.append(
            f"- **{names.get(sid, sid)}**: {change:+d} bikes in {n_min} min "
            f"(riders usually ~{expected:.1f}): {trace}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["pull", "push", "push-models", "pull-okf-log", "prune", "report"]
    )
    parser.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args()
    if args.command == "pull":
        pull(args.work)
    elif args.command == "push":
        push(args.work)
    elif args.command == "push-models":
        push_models(args.work)
    elif args.command == "pull-okf-log":
        pull_okf_log(args.work)
    elif args.command == "prune":
        prune()
    else:
        text = report(args.work)
        print(text)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as f:
                f.write(text + "\n")


if __name__ == "__main__":
    main()
