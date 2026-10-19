"""Step 2 acceptance checks over the raw historical data (see docs/PLAN.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from rebalancer.backfill import db, trips, weather

STATION_MATCH_TARGET = 0.97


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def trip_checks(raw: Path, months: list[str], live_short_names: set[str]) -> list[Check]:
    results: list[Check] = []
    present = [m for m in months if trips.read_manifest(raw, m)]
    missing = sorted(set(months) - set(present))
    results.append(
        Check(
            "Every month downloaded",
            not missing,
            f"{len(present)}/{len(months)} months" + (f"; missing {missing}" if missing else ""),
        )
    )
    if not present:
        return results

    mismatched = []
    for m in present:
        man = trips.read_manifest(raw, m)
        if man.csv_rows != man.parquet_rows:
            mismatched.append(f"{m}: csv {man.csv_rows:,} vs parquet {man.parquet_rows:,}")
    total = sum(trips.read_manifest(raw, m).parquet_rows for m in present)
    results.append(
        Check(
            "Trip counts match Citi Bike's files",
            not mismatched,
            "; ".join(mismatched)
            if mismatched
            else f"{total:,} trips across {len(present)} months",
        )
    )

    glob = (raw / "trips").as_posix() + "/month=*/trips.parquet"
    con = db.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{glob}', hive_partitioning = true)")

    n = con.execute("SELECT count(*) FROM t").fetchone()[0]
    # Citi Bike lists a ride that crosses midnight at month end in both months' files
    # (found 2026-10-19: 512 such rides). That's expected and removed in Step 3; a
    # duplicate *within* one month's file would be a real problem.
    within, across = con.execute(
        """
        WITH d AS (SELECT ride_id, count(*) AS copies, count(DISTINCT month) AS months
                   FROM t GROUP BY ride_id HAVING count(*) > 1)
        SELECT count(*) FILTER (WHERE copies > months), count(*) FILTER (WHERE copies = months)
        FROM d
        """
    ).fetchone()
    results.append(
        Check(
            "No trip appears twice in the same month",
            within == 0,
            f"{within:,} duplicates within a month; {across:,} rides listed in two months "
            "(month-end rides, expected; removed in Step 3)",
        )
    )

    con.register("live", _names_table(live_short_names))
    with_id, matched = con.execute(
        """
        SELECT count(start_station_id), count(live.short_name)
        FROM t LEFT JOIN live ON t.start_station_id = live.short_name
        """
    ).fetchone()
    ratio = matched / with_id if with_id else 0.0
    no_id = n - with_id
    results.append(
        Check(
            f"At least {STATION_MATCH_TARGET:.0%} of trips start at a known station",
            ratio >= STATION_MATCH_TARGET,
            f"{ratio:.2%} of trips with a start station match the live feed "
            f"({no_id:,} trips, {no_id / n:.2%}, have no start station)",
        )
    )

    # Some real IDs have one decimal (e.g. 6131.1 is E 24 St & Park Ave S), so the
    # guarantee we check is the storage type: text keeps "7293.10" intact.
    id_columns = ("ride_id", "start_station_id", "end_station_id")
    not_text = sorted(
        f"{m}:{col}"
        for m in present
        for col in id_columns
        if str(pq.read_schema(trips.month_dir(raw, m) / "trips.parquet").field(col).type)
        != "string"
    )
    results.append(
        Check(
            "Station IDs kept exactly as written (stored as text)",
            not not_text,
            "all ID columns are text" if not not_text else f"not text: {not_text}",
        )
    )

    outside = con.execute(
        "SELECT count(*) FROM t WHERE strftime(started_at, '%Y-%m') <> month"
    ).fetchone()[0]
    results.append(
        Check(
            "Trips are filed under the month they started",
            outside / n < 0.001,
            f"{outside:,} trips ({outside / n:.3%}) start outside their file's month",
        )
    )
    con.close()
    return results


def weather_checks(raw: Path) -> list[Check]:
    out = []
    for kind in weather.ENDPOINTS:
        path = raw / "weather" / f"{kind}.parquet"
        if not path.exists():
            out.append(Check(f"Weather ({kind}) has every hour", False, "file missing"))
            continue
        table = pq.read_table(path)
        gaps = weather.missing_hours(table)
        times = table.column("time_utc").to_pylist()
        out.append(
            Check(
                f"Weather ({kind}) has every hour",
                not gaps,
                f"{table.num_rows:,} hours from {min(times):%Y-%m-%d} to {max(times):%Y-%m-%d}; "
                f"{len(gaps)} missing",
            )
        )
    return out


def holiday_checks(raw: Path, years: list[int]) -> list[Check]:
    path = raw / "holidays" / "holidays.parquet"
    if not path.exists():
        return [Check("Holidays include the obvious ones", False, "file missing")]
    rows = pq.read_table(path).to_pylist()
    dates = {r["date"] for r in rows}
    names = {r["name"] for r in rows}
    missing = [f"{y}-01-01" for y in years if date(y, 1, 1) not in dates]
    missing += [f"{y}-07-04" for y in years if date(y, 7, 4) not in dates]
    if "Thanksgiving" not in names and "Thanksgiving Day" not in names:
        missing.append("Thanksgiving")
    return [
        Check(
            "Holidays include the obvious ones",
            not missing,
            f"{len(rows)} holidays for {years[0]}–{years[-1]}"
            + (f"; missing {missing}" if missing else ""),
        )
    ]


def render(checks: list[Check]) -> str:
    lines = ["| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c.name} | {'✅' if c.passed else '❌'} | {c.detail} |" for c in checks]
    return "\n".join(lines)


def _names_table(names: set[str]):
    import pyarrow as pa

    return pa.table({"short_name": pa.array(sorted(names), type=pa.string())})
