"""Download and check the historical data for Step 2.

backfill all                               # trips + weather + holidays, then checks
backfill trips --start 2025-10 --end 2026-09
backfill weather --start 2025-10-01 --end 2026-10-18
backfill holidays
backfill profiles                          # station-hour demand profile (needs trips + holidays)
backfill check
backfill all --hf    # pull/push data to the Hugging Face dataset in HF_HISTORY_REPO
                     # (used by the GitHub workflow, so nothing heavy runs on a laptop)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from rebalancer.backfill import checks, holidays_calendar, profiles, trips, weather
from rebalancer.config import Settings
from rebalancer.ingest.gbfs import StationInformation, discover_feeds, fetch_json, parse_feed

log = logging.getLogger("backfill")

DEFAULT_START, DEFAULT_END = "2025-10", "2026-09"

Push = Callable[[Path], None]


def _no_push(_: Path) -> None:
    pass


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(60, read=300),
        follow_redirects=True,
        headers={"User-Agent": "citibike-rebalancing research backfill"},
    )


def _years(start: str, end: str) -> list[int]:
    return list(range(int(start[:4]), int(end[:4]) + 2))  # +1 year so live data is covered


def cmd_trips(raw: Path, start: str, end: str, force: bool, push: Push = _no_push) -> None:
    with _client() as client:
        sources = trips.list_sources(client)
        for month in trips.months_between(start, end):
            source = sources.get(month)
            if source is None:
                log.error("%s: no file published by Citi Bike", month)
                continue
            if not force and trips.is_current(raw, month, source):
                log.info("%s: already downloaded, skipping", month)
                continue
            trips.backfill_month(client, raw, month, source)
            push(trips.month_dir(raw, month))


def cmd_weather(raw: Path, start: str, end: str, push: Push = _no_push) -> None:
    with _client() as client:
        rows = weather.backfill(client, raw, start, end)
    log.info("weather rows: %s", rows)
    push(raw / "weather")


def cmd_holidays(raw: Path, years: list[int], push: Push = _no_push) -> None:
    log.info("holidays: %d rows", holidays_calendar.backfill(raw, years))
    push(raw / "holidays")


def cmd_profiles(raw: Path, start: str, end: str, push: Push = _no_push) -> None:
    log.info("profiles: %d rows", profiles.build(raw, start, end))
    push(raw / "profiles")


def live_short_names() -> set[str]:
    s = Settings()
    with _client() as client:
        feeds = discover_feeds(client, s.gbfs_discovery_url, s.gbfs_language)
        result = fetch_json(client, feeds["station_information"], s.http_retries)
    if not result.ok:
        raise SystemExit(f"could not fetch station list: {result.error}")
    snap = parse_feed(result.payload, StationInformation)
    return {st.short_name for st in snap.stations if st.short_name}


def cmd_check(raw: Path, start: str, end: str, push: Push = _no_push) -> bool:
    results = (
        checks.trip_checks(raw, trips.months_between(start, end), live_short_names())
        + checks.weather_checks(raw)
        + checks.holiday_checks(raw, _years(start, end))
        + checks.profile_checks(raw)
    )
    report = (
        f"# Step 2 checks ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n\n{checks.render(results)}\n"
    )
    print(report)
    (raw / "_checks.md").write_text(report)
    push(raw / "_checks.md")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report)
    return all(c.passed for c in results)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "command", choices=["all", "trips", "weather", "holidays", "profiles", "check"]
    )
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--start", help="YYYY-MM for trips, YYYY-MM-DD for weather")
    parser.add_argument("--end", help="YYYY-MM for trips, YYYY-MM-DD for weather")
    parser.add_argument("--force", action="store_true", help="re-download months already present")
    parser.add_argument(
        "--hf", action="store_true", help="pull from / push to the Hugging Face dataset"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    raw = args.raw
    month_start = (args.start or DEFAULT_START)[:7]
    month_end = (args.end or DEFAULT_END)[:7]
    weather_start = args.start if args.start and len(args.start) == 10 else f"{month_start}-01"
    weather_end = (
        args.end
        if args.end and len(args.end) == 10
        else (date.today() - timedelta(days=1)).isoformat()
    )

    push: Push = _no_push
    if args.hf:
        from rebalancer.backfill.hub import HubSync

        repo = os.environ.get("HF_HISTORY_REPO", "citibike-history")
        token = os.environ.get("HF_TOKEN")
        if not token:
            sys.exit("HF_TOKEN must be set to use --hf")
        sync = HubSync(repo, token, raw)
        sync.pull()
        push = sync.push

    if args.command in ("trips", "all"):
        cmd_trips(raw, month_start, month_end, args.force, push)
    if args.command in ("weather", "all"):
        cmd_weather(raw, weather_start, weather_end, push)
    if args.command in ("holidays", "all"):
        cmd_holidays(raw, _years(month_start, month_end), push)
    if args.command in ("profiles", "all"):
        cmd_profiles(raw, month_start, month_end, push)
    if args.command in ("check", "all") and not cmd_check(raw, month_start, month_end, push):
        sys.exit(1)


if __name__ == "__main__":
    main()
