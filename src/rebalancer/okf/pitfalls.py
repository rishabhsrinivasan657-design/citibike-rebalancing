"""Trap analysis (Step 8.6): how wrong do answers get without the bundle's definitions?

No AI involved. For each question, the correct answer comes from the approved metric
(Attested Computation); each "trap" is a plausible query someone without the
definitions would write, with exactly one mistake. Same day, zone and ranking, so any
difference is caused by that mistake alone.

    okf-pitfalls --bundle okf --data marts --out okf/PITFALLS.md
"""

from __future__ import annotations

import argparse
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from rebalancer.okf.attest import _table_path
from rebalancer.okf.evaluate import recorded_days, trip_months
from rebalancer.okf.knowledge import KnowledgeBase

TRAPS = {
    "utc_dates": "Using UTC dates (days end at 8pm New York time)",
    "count_recorder_down": "Counting minutes when the recorder was down",
    "ignore_renting": "Ignoring whether the station is renting",
    "ignore_returning": "Ignoring whether the station accepts returns",
    "minutes_with_data": "Coverage as 'minutes that have data'",
    "simple_van_rule": "A simple '6+ bikes in 3 minutes' van rule, all regions",
    "numeric_station_codes": "Station codes treated as numbers",
}


def zone_filter(alias: str = "s") -> str:
    return f"""(
        ? = 'all' OR EXISTS (
            SELECT 1 FROM dim_zone AS z
            WHERE z.zone = lower(?)
              AND 6371000 * sqrt(
                    pow(radians({alias}.lat - z.center_lat), 2)
                    + pow(radians({alias}.lon - z.center_lon) * cos(radians(z.center_lat)), 2)
                  ) <= z.radius_m
        )
    )"""


@dataclass
class Case:
    question: str
    kind: str  # station | number
    correct: object
    correct_value: float | None
    traps: dict[str, tuple[object, float | None]] = field(default_factory=dict)


class Traps:
    def __init__(self, data: Path) -> None:
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")
        for t in ("fct_station_minute", "dim_station", "dim_zone", "agg_station_month"):
            p = _table_path(data, t)
            self.con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{p.as_posix()}')")
        self.con.execute(
            """CREATE VIEW m AS
               SELECT f.*, s.name AS station_name, s.lat, s.lon, s.region_id,
                      CAST(timezone('America/New_York', f.minute) AS DATE) AS local_date,
                      CAST(f.minute AS DATE) AS utc_date
               FROM fct_station_minute AS f
               JOIN (SELECT * FROM dim_station WHERE is_current) AS s USING (station_id)"""
        )

    def _top(self, where: str, params: list, zone: str) -> tuple[list[str], float | None]:
        rows = self.con.execute(
            f"""SELECT station_name, count(*) AS n FROM m AS s
                WHERE {where} AND {zone_filter()}
                GROUP BY 1 ORDER BY n DESC, station_name LIMIT 50""",
            [*params, zone, zone],
        ).fetchall()
        if not rows:
            return ["none"], 0.0
        best = rows[0][1]
        return [r[0] for r in rows if r[1] == best], float(best)

    def empty(self, day: str, zone: str, trap: str):
        where = {
            "utc_dates": "utc_date = CAST(? AS DATE) AND recorder_up AND is_empty",
            "count_recorder_down": "local_date = CAST(? AS DATE) AND is_empty",
            "ignore_renting": "local_date = CAST(? AS DATE) AND recorder_up AND num_bikes_available = 0",
        }[trap]
        return self._top(where, [day], zone)

    def full(self, day: str, zone: str, trap: str):
        where = {
            "utc_dates": "utc_date = CAST(? AS DATE) AND recorder_up AND is_full",
            "count_recorder_down": "local_date = CAST(? AS DATE) AND is_full",
            "ignore_returning": "local_date = CAST(? AS DATE) AND recorder_up AND num_docks_available = 0",
        }[trap]
        return self._top(where, [day], zone)

    def coverage_minutes_with_data(self, day: str) -> float:
        (n,) = self.con.execute(
            "SELECT count(DISTINCT minute) FROM m WHERE local_date = CAST(? AS DATE)", [day]
        ).fetchone()
        (window,) = self.con.execute(
            """SELECT count(DISTINCT minute) FROM fct_station_minute
               WHERE CAST(timezone('America/New_York', minute) AS DATE) = CAST(? AS DATE)""",
            [day],
        ).fetchone()
        return round(n / window, 4) if window else 0.0

    def vans_simple_rule(self, day: str, zone: str) -> dict[str, float]:
        """Rule v1 from Step 3: any 6+ one-way change within 3 minutes, every region."""
        row = self.con.execute(
            f"""
            WITH pm AS (
                SELECT station_id, minute, recorder_up, lat, lon, local_date,
                       num_bikes_available + num_bikes_disabled
                         - lag(num_bikes_available + num_bikes_disabled) OVER w AS delta,
                       lag(recorder_up) OVER w AS prev_up
                FROM m WINDOW w AS (PARTITION BY station_id ORDER BY minute)
            ),
            moves AS (SELECT *, sign(delta) AS d FROM pm
                      WHERE recorder_up AND prev_up AND delta <> 0),
            runs AS (
                SELECT *, sum(new_run) OVER (PARTITION BY station_id ORDER BY minute) AS run_id
                FROM (SELECT *, CASE WHEN lag(minute) OVER w2 = minute - INTERVAL 1 MINUTE
                                      AND lag(d) OVER w2 = d THEN 0 ELSE 1 END AS new_run
                      FROM moves WINDOW w2 AS (PARTITION BY station_id ORDER BY minute))
            ),
            visits AS (
                SELECT station_id, any_value(lat) AS lat, any_value(lon) AS lon,
                       min(local_date) AS local_date, sum(delta) AS change
                FROM runs GROUP BY station_id, run_id
                HAVING abs(sum(delta)) >= 6 AND count(*) <= 3
            )
            SELECT count(*) FILTER (WHERE change > 0),
                   count(*) FILTER (WHERE change < 0),
                   coalesce(sum(change) FILTER (WHERE change > 0), 0),
                   count(DISTINCT station_id)
            FROM visits AS s
            WHERE local_date = CAST(? AS DATE) AND {zone_filter()}
            """,
            [day, zone, zone],
        ).fetchone()
        return dict(
            zip(
                ["drop_off_visits", "pick_up_visits", "bikes_dropped_off", "stations_visited"],
                map(float, row),
                strict=True,
            )
        )

    def busiest_numeric_codes(self, m1: str, m2: str, zone: str):
        rows = self.con.execute(
            f"""SELECT any_value(station_name), sum(rides_started + rides_ended) AS n
                FROM agg_station_month AS s
                WHERE month BETWEEN ? AND ? AND lat IS NOT NULL AND {zone_filter()}
                GROUP BY try_cast(short_name AS DOUBLE)
                ORDER BY n DESC LIMIT 50""",
            [m1, m2, zone, zone],
        ).fetchall()
        if not rows:
            return ["none"], 0.0
        best = rows[0][1]
        return [r[0] for r in rows if r[1] == best], float(best)


def _correct_top(kb: KnowledgeBase, comp: str, key: str, **p):
    out = kb.run_metric(comp, {k: str(v) for k, v in {**p, "top_n": 50}.items()})
    rows = out["result"] or []
    if not rows:
        return ["none"], 0.0
    best = rows[0][key]
    return [r["station_name"] for r in rows if r[key] == best], float(best)


def _correct_rows(kb: KnowledgeBase, comp: str, **p) -> list[dict]:
    return kb.run_metric(comp, {k: str(v) for k, v in p.items()})["result"] or []


def build_cases(kb: KnowledgeBase, data: Path) -> list[Case]:
    t = Traps(data)
    days = recorded_days(kb)
    d1, d2 = days[0], days[1] if len(days) > 1 else days[0]
    months = trip_months(data)
    m_last, m_prev = months[-1], months[-2] if len(months) > 1 else months[-1]
    cases: list[Case] = []

    for day, zone in ((d1, "midtown"), (d1, "all"), (d2, "downtown")):
        names, v = _correct_top(
            kb, "empty-stations", "empty_minutes", start_date=day, end_date=day, zone=zone
        )
        c = Case(f"Station empty longest, {zone}, {day}", "station", names, v)
        for trap in ("utc_dates", "count_recorder_down", "ignore_renting"):
            c.traps[trap] = t.empty(day, zone, trap)
        cases.append(c)
    for day, zone in ((d1, "midtown"), (d2, "all"), (d1, "union square")):
        names, v = _correct_top(
            kb, "full-stations", "full_minutes", start_date=day, end_date=day, zone=zone
        )
        c = Case(f"Station full longest, {zone}, {day}", "station", names, v)
        for trap in ("utc_dates", "count_recorder_down", "ignore_returning"):
            c.traps[trap] = t.full(day, zone, trap)
        cases.append(c)
    for day, zone, key, label in (
        (d1, "midtown", "drop_off_visits", "Van drop-off visits"),
        (d2, "all", "pick_up_visits", "Van pickup visits"),
        (d1, "all", "bikes_dropped_off", "Bikes dropped off by vans"),
        (d2, "all", "stations_visited", "Stations visited by vans"),
    ):
        rows = _correct_rows(kb, "van-activity", start_date=day, end_date=day, zone=zone)
        v = float(rows[0][key]) if rows else 0.0
        c = Case(f"{label}, {zone}, {day}", "number", v, v)
        naive = t.vans_simple_rule(day, zone)[key]
        c.traps["simple_van_rule"] = (naive, naive)
        cases.append(c)
    for day in dict.fromkeys((d1, d2)):
        rows = _correct_rows(kb, "recorder-coverage", start_date=day, end_date=day)
        v = float(rows[0]["coverage"]) if rows else 0.0
        c = Case(f"Recorder coverage, {day}", "number", v, v)
        naive = t.coverage_minutes_with_data(day)
        c.traps["minutes_with_data"] = (naive, naive)
        cases.append(c)
    for m1, m2, zone in (
        (m_last, m_last, "all"),
        (m_prev, m_last, "midtown"),
        (m_last, m_last, "williamsburg"),
    ):
        names, v = _correct_top(
            kb, "busiest-stations", "rides_total", start_month=m1, end_month=m2, zone=zone
        )
        c = Case(f"Busiest station, {zone}, {m1}..{m2}", "station", names, v)
        c.traps["numeric_station_codes"] = t.busiest_numeric_codes(m1, m2, zone)
        cases.append(c)
    return cases


def changed(case: Case, trap: str) -> bool:
    answer, value = case.traps[trap]
    if case.kind == "station":
        return not set(answer) & set(case.correct) or value != case.correct_value
    return abs(float(answer) - float(case.correct)) > 1e-9


def pct_error(case: Case, trap: str) -> float | None:
    _, value = case.traps[trap]
    if value is None or not case.correct_value:
        return None
    return abs(value - case.correct_value) / abs(case.correct_value)


def report(cases: list[Case]) -> str:
    lines = [
        "# Trap analysis: how wrong do answers get without the definitions?",
        "",
        "No AI involved. For each question, the **correct** answer comes from the approved "
        "metric; each **trap** is the same query with one common mistake someone without the "
        "bundle's definitions would make. Generated by `okf-pitfalls`.",
        "",
        "| Mistake | Answers changed | Typical error when changed |",
        "|---|---|---|",
    ]
    for trap, label in TRAPS.items():
        hit = [c for c in cases if trap in c.traps]
        if not hit:
            continue
        ch = [c for c in hit if changed(c, trap)]
        errs = [e for c in ch if (e := pct_error(c, trap)) is not None]
        typical = f"{statistics.median(errs):.0%} off (median)" if errs else "–"
        lines.append(f"| {label} | **{len(ch)} of {len(hit)}** | {typical} |")
    lines += [
        "",
        "## Per question",
        "",
        "| Question | Correct (approved metric) | Mistake | Mistaken answer | Changed? |",
        "|---|---|---|---|---|",
    ]

    def fmt(kind, ans, val):
        if kind == "station":
            name = " / ".join(ans[:2]) + (" …" if len(ans) > 2 else "")
            return f"{name} ({val:g})" if val is not None else name
        return f"{float(ans):g}"

    for c in cases:
        for trap, (ans, val) in c.traps.items():
            lines.append(
                f"| {c.question} | {fmt(c.kind, c.correct, c.correct_value)} | {TRAPS[trap]} "
                f"| {fmt(c.kind, ans, val)} | {'**yes**' if changed(c, trap) else 'no'} |"
            )
    lines += [
        "",
        "This measures the traps, not an agent: it shows how much each definition matters on "
        "this data. Whether an agent actually avoids them is what the agent eval "
        "(`okf-eval`, needs an Anthropic API key) measures.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("okf/PITFALLS.md"))
    args = parser.parse_args()
    text = report(build_cases(KnowledgeBase(args.bundle, args.data), args.data))
    args.out.write_text(text)
    print(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(text)


if __name__ == "__main__":
    main()
