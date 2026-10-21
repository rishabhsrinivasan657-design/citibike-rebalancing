"""Eval (Step 8.5): does the OKF bundle help an agent answer correctly?

Same Claude model, same questions, two arms:
- with_okf:    the four MCP tools (search, read, list metrics, run approved metric)
- without_okf: table names and column types, and a raw read-only DuckDB SQL tool

Expected answers come from the approved metrics run by this script, not by hand.

    okf-eval --bundle okf --data marts --out okf/EVAL.md       # needs ANTHROPIC_API_KEY
    okf-eval ... --model claude-sonnet-5-5                     # cheaper model
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from rebalancer.okf.attest import TABLES, _table_path
from rebalancer.okf.knowledge import KnowledgeBase
from rebalancer.okf.server import INSTRUCTIONS

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TURNS = 15
ANSWER_FORMAT = """
When you are done, end your reply with exactly one final line:
ANSWER: <the answer>
Use just a station name, or just a number, as the question asks. If you cannot answer
reliably, the final line must be: ANSWER: CANNOT_ANSWER
"""
DECLINE = "CANNOT_ANSWER"


@dataclass
class Question:
    id: str
    text: str
    kind: str  # station | number | share | decline
    expected: list[str] | float | None  # acceptable names, a number, or None
    metric: str | None = None


@dataclass
class Run:
    question: str
    arm: str
    answer: str | None
    correct: bool
    cited: bool
    passed_warnings: bool | None
    tool_calls: int
    refused: bool = False
    error: str | None = None
    transcript_tail: str = ""


# ---------------------------------------------------------------- questions
def _metric(kb: KnowledgeBase, comp: str, **params) -> list[dict]:
    out = kb.run_metric(comp, {k: str(v) for k, v in params.items()})
    if not out["ok"]:
        raise RuntimeError(f"{comp}: {out.get('error') or out['verdict']}")
    return out["result"]


def _top(rows: list[dict], key: str) -> tuple[list[str], float | None]:
    """All station names tied for the top value (any of them is a correct answer)."""
    if not rows:
        return ["none"], None
    best = rows[0][key]
    return [r["station_name"] for r in rows if r[key] == best], best


def recorded_days(kb: KnowledgeBase) -> list[str]:
    """Days to ask about, best first: fully recorded days (most recent first), then
    partly recorded ones by minutes captured, so two different days are used whenever
    the recorder has more than one."""
    rows = _metric(kb, "recorder-coverage", start_date="2000-01-01", end_date="2100-01-01")
    full = [
        r["local_date"] for r in rows if r["coverage"] >= 0.95 and r["minutes_in_window"] >= 1380
    ]
    rest = [
        r["local_date"]
        for r in sorted(rows, key=lambda r: -r["minutes_recorded"])
        if r["local_date"] not in full
    ]
    return sorted(full, reverse=True) + rest


def trip_months(data: Path) -> list[str]:
    path = _table_path(data, "agg_station_month")
    con = duckdb.connect()
    months = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT month FROM read_parquet('{path.as_posix()}') ORDER BY 1"
        ).fetchall()
    ]
    con.close()
    return months


def build_questions(kb: KnowledgeBase, data: Path) -> list[Question]:
    days = recorded_days(kb)
    d1, d2 = days[0], days[1] if len(days) > 1 else days[0]
    months = trip_months(data)
    m_last = months[-1]
    m_prev = months[-2] if len(months) > 1 else m_last
    qs: list[Question] = []

    def station(qid, text, comp, key, **p):
        names, value = _top(_metric(kb, comp, top_n=50, **p), key)
        qs.append(Question(qid, text, "station", names, comp))
        return names, value

    def number(qid, text, value, comp, kind="number"):
        qs.append(Question(qid, text, kind, float(value or 0), comp))

    _, v = station(
        "empty-midtown-d1",
        f"Which station in Midtown was empty (no bike to rent) for the most recorded minutes on {d1}?",
        "empty-stations",
        "empty_minutes",
        start_date=d1,
        end_date=d1,
        zone="midtown",
    )
    number(
        "empty-midtown-d1-minutes",
        f"How many recorded minutes was the Midtown station that was empty longest on {d1} empty that day?",
        v,
        "empty-stations",
    )
    station(
        "empty-midtown-d2",
        f"Which Midtown station had no bikes available for the longest on {d2}?",
        "empty-stations",
        "empty_minutes",
        start_date=d2,
        end_date=d2,
        zone="midtown",
    )
    station(
        "empty-all-d1",
        f"Across the whole Citi Bike system, which station was empty the longest on {d1}?",
        "empty-stations",
        "empty_minutes",
        start_date=d1,
        end_date=d1,
        zone="all",
    )
    _, v = station(
        "empty-downtown-d2",
        f"In the Downtown (Financial District) zone, which station was empty for the most minutes on {d2}?",
        "empty-stations",
        "empty_minutes",
        start_date=d2,
        end_date=d2,
        zone="downtown",
    )
    number(
        "empty-downtown-d2-minutes",
        f"For that Downtown station empty longest on {d2}, how many recorded minutes was it empty?",
        v,
        "empty-stations",
    )
    _, v = station(
        "full-midtown-d1",
        f"Which Midtown station was full (no free dock) for the most recorded minutes on {d1}?",
        "full-stations",
        "full_minutes",
        start_date=d1,
        end_date=d1,
        zone="midtown",
    )
    number(
        "full-midtown-d1-minutes",
        f"How many recorded minutes was the Midtown station that was full longest on {d1} full that day?",
        v,
        "full-stations",
    )
    _, v = station(
        "full-all-d2",
        f"Which station in the whole system had no free docks for the longest on {d2}?",
        "full-stations",
        "full_minutes",
        start_date=d2,
        end_date=d2,
        zone="all",
    )
    number(
        "full-all-d2-minutes",
        f"How many recorded minutes was the system-wide longest-full station full on {d2}?",
        v,
        "full-stations",
    )
    station(
        "full-union-d1",
        f"In the Union Square zone, which station was full the longest on {d1}?",
        "full-stations",
        "full_minutes",
        start_date=d1,
        end_date=d1,
        zone="union square",
    )

    vans_mid = _metric(kb, "van-activity", start_date=d1, end_date=d1, zone="midtown")
    vans_all_d1 = _metric(kb, "van-activity", start_date=d1, end_date=d1, zone="all")
    vans_all_d2 = _metric(kb, "van-activity", start_date=d2, end_date=d2, zone="all")
    first = lambda rows, k: rows[0][k] if rows else 0  # noqa: E731
    number(
        "vans-dropoffs-midtown-d1",
        f"How many likely Lyft van drop-off visits were there at Midtown stations on {d1}?",
        first(vans_mid, "drop_off_visits"),
        "van-activity",
    )
    number(
        "vans-pickups-all-d2",
        f"How many likely van pickup visits were there across the whole system on {d2}?",
        first(vans_all_d2, "pick_up_visits"),
        "van-activity",
    )
    number(
        "vans-bikes-all-d1",
        f"How many bikes did likely van drop-offs add across the whole system on {d1}?",
        first(vans_all_d1, "bikes_dropped_off"),
        "van-activity",
    )
    number(
        "vans-stations-all-d2",
        f"How many different stations had at least one likely van visit on {d2}, system-wide?",
        first(vans_all_d2, "stations_visited"),
        "van-activity",
    )

    cov = {
        r["local_date"]: r["coverage"]
        for r in _metric(kb, "recorder-coverage", start_date=d2, end_date=d1)
    }
    number(
        "coverage-d1",
        f"What share of minutes did the live recorder capture on {d1}? Give a fraction between 0 and 1.",
        cov.get(d1),
        "recorder-coverage",
        "share",
    )
    number(
        "coverage-d2",
        f"What was the recorder's coverage (fraction of minutes captured) on {d2}?",
        cov.get(d2),
        "recorder-coverage",
        "share",
    )

    station(
        "busiest-all-last",
        f"Which station had the most rides started plus ended in {m_last}?",
        "busiest-stations",
        "rides_total",
        start_month=m_last,
        end_month=m_last,
        zone="all",
    )
    station(
        "busiest-midtown-two",
        f"Which Midtown station had the most rides (started plus ended) over {m_prev} to {m_last}?",
        "busiest-stations",
        "rides_total",
        start_month=m_prev,
        end_month=m_last,
        zone="midtown",
    )
    _, v = station(
        "busiest-williamsburg-last",
        f"Which Williamsburg station was busiest by rides started plus ended in {m_last}?",
        "busiest-stations",
        "rides_total",
        start_month=m_last,
        end_month=m_last,
        zone="williamsburg",
    )
    number(
        "busiest-williamsburg-last-rides",
        f"How many rides started or ended at the busiest Williamsburg station in {m_last}?",
        v,
        "busiest-stations",
    )

    for qid, text in (
        (
            "decline-riders-turned-away",
            f"How many riders were turned away at Midtown stations on {d1} because there was no bike?",
        ),
        (
            "decline-forecast",
            "How many rides will start at the Grand Central stations next Friday?",
        ),
        ("decline-member-duration", f"What was the average ride duration for members on {d1}?"),
        ("decline-driver", f"Which Lyft van driver made the most rebalancing visits on {d1}?"),
    ):
        qs.append(Question(qid, text, "decline", None))
    return qs


# ------------------------------------------------------------------ grading
def final_answer(text: str) -> str | None:
    m = re.findall(r"^\s*ANSWER:\s*(.+?)\s*$", text, re.M)
    return m[-1] if m else None


def grade(q: Question, answer: str | None) -> bool:
    if answer is None:
        return False
    if q.kind == "decline":
        return DECLINE in answer.upper()
    if DECLINE in answer.upper():
        return False
    if q.kind == "station":
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
        return any(norm(name) and norm(name) in norm(answer) for name in q.expected)
    nums = re.findall(r"-?\d[\d,]*\.?\d*", answer)
    if not nums:
        return False
    value = float(nums[0].replace(",", ""))
    if q.kind == "share":
        if value > 1 and "%" in answer:
            value /= 100
        return abs(value - float(q.expected)) <= 0.005
    return abs(value - float(q.expected)) <= 0.5


# -------------------------------------------------------------------- arms
def okf_tools() -> list[dict]:
    return [
        {
            "name": "search_concepts",
            "description": "Search tables, metrics, computations and glossary terms by keywords. Optional type filter, e.g. 'Metric', 'Table', 'Glossary Term'.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "type": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "read_concept",
            "description": "Read one concept by id (e.g. 'tables/agg_station_day'), with its trust tier and staleness.",
            "input_schema": {
                "type": "object",
                "properties": {"concept_id": {"type": "string"}},
                "required": ["concept_id"],
            },
        },
        {
            "name": "list_metrics",
            "description": "The approved metrics (Attested Computations) and their parameters.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "run_metric",
            "description": "Run an approved metric, e.g. computation='computations/empty-stations' with parameters as strings. Returns result, receipt, verdict and warnings; only report results when ok is true.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "computation": {"type": "string"},
                    "parameters": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "required": ["computation", "parameters"],
            },
        },
    ]


def sql_tools() -> list[dict]:
    return [
        {
            "name": "list_tables",
            "description": "Names and column types of the tables in the database.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "run_sql",
            "description": "Run a read-only DuckDB SQL query and get up to 200 rows back as JSON.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    ]


class SqlArm:
    """The baseline: raw tables, no definitions, no approved metrics."""

    def __init__(self, data: Path) -> None:
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")
        self.tables = []
        for t in TABLES:
            try:
                p = _table_path(data, t)
            except Exception:
                continue
            src = (
                f"read_parquet('{p.as_posix()}/**/*.parquet', hive_partitioning = true)"
                if p.is_dir()
                else f"read_parquet('{p.as_posix()}')"
            )
            self.con.execute(f"CREATE VIEW {t} AS SELECT * FROM {src}")
            self.tables.append(t)

    def call(self, name: str, args: dict) -> str:
        if name == "list_tables":
            out = {}
            for t in self.tables:
                out[t] = [
                    f"{c} {ty}"
                    for c, ty in self.con.execute(
                        f"SELECT column_name, column_type FROM (DESCRIBE {t})"
                    ).fetchall()
                ]
            return json.dumps(out)
        if name == "run_sql":
            q = args.get("query", "")
            if re.search(
                r"\b(create|insert|update|delete|drop|alter|copy|attach|install|load)\b", q, re.I
            ):
                return json.dumps({"error": "read-only: only SELECT queries are allowed"})
            cur = self.con.cursor()
            try:
                res = cur.execute(q)
                cols = [d[0] for d in res.description]
                rows = [dict(zip(cols, r, strict=True)) for r in res.fetchmany(200)]
                return json.dumps(rows, default=str)
            except duckdb.Error as exc:
                return json.dumps({"error": str(exc)[:500]})
        return json.dumps({"error": f"unknown tool {name}"})


class OkfArm:
    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        self.saw_warnings = False

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "search_concepts":
                out = self.kb.search(args.get("query", ""), args.get("type"))
            elif name == "read_concept":
                out = self.kb.read(args.get("concept_id", ""))
            elif name == "list_metrics":
                out = self.kb.list_metrics()
            elif name == "run_metric":
                out = self.kb.run_metric(args.get("computation", ""), args.get("parameters") or {})
                self.saw_warnings = self.saw_warnings or bool(out.get("warnings"))
            else:
                out = {"error": f"unknown tool {name}"}
        except KeyError as exc:
            out = {"error": str(exc)}
        return json.dumps(out, default=str)


SQL_SYSTEM = """\
You answer questions about Citi Bike in New York using a DuckDB database. Use
list_tables and run_sql. Dates in questions are New York calendar dates.
"""


def ask(
    client, model: str, system: str, tools: list[dict], arm, question: str
) -> tuple[str, int, bool]:
    """Manual tool-use loop. Returns (final text, tool calls, refused)."""
    messages = [{"role": "user", "content": question}]
    calls = 0
    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system + ANSWER_FORMAT,
            tools=tools,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "", calls, True
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            text = "\n".join(b.text for b in response.content if b.type == "text")
            return text, calls, False
        results = []
        for u in uses:
            calls += 1
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": u.id,
                    "content": arm.call(u.name, dict(u.input or {})),
                }
            )
        messages.append({"role": "user", "content": results})
    return "", calls, False


def run_eval(
    client, kb: KnowledgeBase, data: Path, questions: list[Question], model: str, workers: int = 4
) -> list[Run]:
    def one(args: tuple[Question, str]) -> Run:
        q, arm_name = args
        arm = OkfArm(kb) if arm_name == "with_okf" else SqlArm(data)
        system = INSTRUCTIONS if arm_name == "with_okf" else SQL_SYSTEM
        tools = okf_tools() if arm_name == "with_okf" else sql_tools()
        try:
            text, calls, refused = ask(client, model, system, tools, arm, q.text)
        except Exception as exc:  # recorded, not hidden: the run counts as wrong
            return Run(
                q.id,
                arm_name,
                None,
                False,
                False,
                None,
                0,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        answer = final_answer(text)
        cited = bool(q.metric) and (
            q.metric in text
            or "result_sha256" in text
            or re.search(r"\b[0-9a-f]{16}\b", text) is not None
        )
        warned = None
        if arm_name == "with_okf" and arm.saw_warnings:
            warned = bool(re.search(r"stale|not been verified|unverified|review", text, re.I))
        return Run(
            q.id,
            arm_name,
            answer,
            grade(q, answer),
            cited,
            warned,
            calls,
            refused,
            transcript_tail=text[-400:],
        )

    jobs = [(q, a) for q in questions for a in ("with_okf", "without_okf")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


def report(questions: list[Question], runs: list[Run], model: str) -> str:
    by = {(r.question, r.arm): r for r in runs}
    lines = [
        "# OKF eval: does the context layer help?",
        "",
        f"Model: `{model}`, same for both arms. {len(questions)} questions; expected answers come "
        "from the approved metrics run by script (`okf-eval`), not by hand.",
        "",
        "- **with OKF**: the bundle's four tools (search, read, list metrics, run approved metric).",
        "- **without OKF**: table names, column types and a raw read-only SQL tool.",
        "",
        "| Arm | Correct | Answerable correct | Correct declines | Cited the metric | Passed on warnings | Avg tool calls |",
        "|---|---|---|---|---|---|---|",
    ]
    answerable = [q for q in questions if q.kind != "decline"]
    declines = [q for q in questions if q.kind == "decline"]
    for arm in ("with_okf", "without_okf"):
        rs = [by[(q.id, arm)] for q in questions if (q.id, arm) in by]
        ok = sum(r.correct for r in rs)
        ans_ok = sum(by[(q.id, arm)].correct for q in answerable if (q.id, arm) in by)
        dec_ok = sum(by[(q.id, arm)].correct for q in declines if (q.id, arm) in by)
        cited = sum(by[(q.id, arm)].cited for q in answerable if (q.id, arm) in by)
        warned = [r.passed_warnings for r in rs if r.passed_warnings is not None]
        warn_txt = f"{sum(warned)}/{len(warned)}" if warned else "–"
        calls = sum(r.tool_calls for r in rs) / max(len(rs), 1)
        lines.append(
            f"| {arm.replace('_', ' ')} | **{ok}/{len(rs)}** | {ans_ok}/{len(answerable)} | "
            f"{dec_ok}/{len(declines)} | {cited}/{len(answerable)} | {warn_txt} | {calls:.1f} |"
        )
    lines += [
        "",
        "## Per question",
        "",
        "| Question | Expected | with OKF | without OKF |",
        "|---|---|---|---|",
    ]
    for q in questions:
        exp = (
            "decline"
            if q.kind == "decline"
            else " / ".join(q.expected[:3])
            if isinstance(q.expected, list)
            else f"{q.expected:g}"
        )
        cells = []
        for arm in ("with_okf", "without_okf"):
            r = by.get((q.id, arm))
            cells.append(
                "–"
                if r is None
                else ("✅ " if r.correct else "❌ ")
                + (r.answer or r.error or "no answer").replace("|", "/")[:60]
            )
        lines.append(
            f"| {q.text.replace('|', '/')} | {exp.replace('|', '/')} | {cells[0]} | {cells[1]} |"
        )
    errors = [r for r in runs if r.error]
    if errors:
        lines += ["", f"{len(errors)} runs failed with an error and are counted as wrong."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("okf/EVAL.md"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--questions-only",
        action="store_true",
        help="print the questions and expected answers; no API calls",
    )
    args = parser.parse_args()

    kb = KnowledgeBase(args.bundle, args.data)
    questions = build_questions(kb, args.data)
    if args.questions_only:
        print(json.dumps([asdict(q) for q in questions], indent=2))
        return
    import anthropic

    runs = run_eval(anthropic.Anthropic(), kb, args.data, questions, args.model)
    text = report(questions, runs, args.model)
    args.out.write_text(text)
    args.out.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": args.model,
                "questions": [asdict(q) for q in questions],
                "runs": [asdict(r) for r in runs],
            },
            indent=2,
            default=str,
        )
    )
    print(text)


if __name__ == "__main__":
    main()
