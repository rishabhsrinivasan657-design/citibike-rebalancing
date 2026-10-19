"""Recording coverage report: what share of minutes have a successful status poll.

Reads `poll_log` from the Hugging Face dataset (or a local lake with --lake) and
checks the Step 1 target: >= 98% of minutes covered over the last 24 hours.

    coverage-report                     # uses HF_REPO and HF_TOKEN
    coverage-report --lake ./data/lake  # local files
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.dataset as ds

TARGET = 0.98


@dataclass
class Coverage:
    start: datetime
    end: datetime
    expected_minutes: int
    covered_minutes: int
    failed_polls: int
    gaps: list[tuple[datetime, int]] = field(default_factory=list)  # (gap start, minutes)

    @property
    def ratio(self) -> float:
        return self.covered_minutes / self.expected_minutes if self.expected_minutes else 0.0

    @property
    def passed(self) -> bool:
        return self.ratio >= TARGET


def compute(
    ok_polls: list[datetime], failed_polls: int, hours: float = 24.0, min_gap_minutes: int = 3
) -> Coverage:
    """Coverage over the `hours` before the newest poll (or since the first, if shorter).

    A minute is covered if any successful station_status poll falls in it. Polls from
    overlapping runs land in the same minute, so they count once.
    """
    if not ok_polls:
        raise ValueError("no successful polls recorded")
    minutes = {p.replace(second=0, microsecond=0) for p in ok_polls}
    end = max(minutes)
    start = max(min(minutes), end - timedelta(hours=hours) + timedelta(minutes=1))
    expected = int((end - start).total_seconds() // 60) + 1

    gaps, gap_start, t = [], None, start
    covered = 0
    while t <= end:
        if t in minutes:
            covered += 1
            if gap_start is not None:
                length = int((t - gap_start).total_seconds() // 60)
                if length >= min_gap_minutes:
                    gaps.append((gap_start, length))
                gap_start = None
        elif gap_start is None:
            gap_start = t
        t += timedelta(minutes=1)
    return Coverage(start, end, expected, covered, failed_polls, gaps)


def render(c: Coverage) -> str:
    verdict = "PASS ✅" if c.passed else "NOT YET ❌"
    hours = c.expected_minutes / 60
    lines = [
        f"## Recording coverage: {verdict}",
        "",
        f"- Window: {c.start:%Y-%m-%d %H:%M} → {c.end:%Y-%m-%d %H:%M} UTC ({hours:.1f} h)",
        f"- Minutes recorded: **{c.covered_minutes:,} of {c.expected_minutes:,} "
        f"({c.ratio:.1%})**, target {TARGET:.0%}",
        f"- Failed polls: {c.failed_polls}",
    ]
    if hours < 23.5:
        lines.append(
            f"- Note: only {hours:.1f} h recorded so far; the target is checked over 24 h."
        )
    if c.gaps:
        lines += ["", "| Gap starts (UTC) | Minutes missed |", "|---|---|"]
        lines += [f"| {s:%Y-%m-%d %H:%M} | {n} |" for s, n in c.gaps]
    else:
        lines.append("- No gaps of 3 minutes or more.")
    return "\n".join(lines)


def load_polls(lake: Path) -> tuple[list[datetime], int]:
    rows = (
        ds.dataset(lake / "poll_log", format="parquet", partitioning="hive")
        .to_table(columns=["polled_at", "feed", "ok"])
        .to_pylist()
    )
    status = [r for r in rows if r["feed"] == "station_status"]
    ok = [r["polled_at"].astimezone(UTC) for r in status if r["ok"]]
    return ok, sum(1 for r in status if not r["ok"])


def download_poll_log(repo: str, token: str, dest: Path) -> Path:
    from huggingface_hub import HfApi, snapshot_download

    from rebalancer.ingest.recorder import resolve_repo_id

    repo_id = resolve_repo_id(HfApi(token=token), repo, need_write=False)
    since = datetime.now(UTC) - timedelta(days=2)
    days = {(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
    snapshot_download(
        repo_id,
        repo_type="dataset",
        token=token,
        local_dir=dest,
        allow_patterns=[f"lake/poll_log/date={d}/**" for d in days],
    )
    return dest / "lake"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake", type=Path, help="read a local lake instead of Hugging Face")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        if args.lake:
            lake = args.lake
        else:
            repo, token = os.environ.get("HF_REPO"), os.environ.get("HF_TOKEN")
            if not repo or not token:
                sys.exit("HF_REPO and HF_TOKEN must be set (or pass --lake)")
            lake = download_poll_log(repo, token, Path(tmp))
        ok, failed = load_polls(lake)

    coverage = compute(ok, failed, hours=args.hours)
    report = render(coverage)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")
    if not coverage.passed:
        sys.exit(1)  # a failed scheduled run makes GitHub email the repo owner


if __name__ == "__main__":
    main()
