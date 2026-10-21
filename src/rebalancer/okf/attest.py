"""Run OKF Attested Computations on DuckDB and check their receipts (Step 8.3).

The executor binds only the declared parameters into the sanctioned SQL, runs it on
the mart Parquet files, and returns a receipt. The attester is deterministic (no
LLM): it checks the SQL that ran is the sanctioned SQL, the parameters are the
declared ones with valid types, the result matches its fingerprint, and (given the
data) that re-running on the same data version reproduces the same result.

    okf-metric list --bundle okf/bundle
    okf-metric run computations/empty-stations --param start_date=2026-10-20 \\
        --param end_date=2026-10-20 --param zone=midtown --param top_n=5 \\
        --bundle okf/bundle --data work/marts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from rebalancer.okf.validate import split_frontmatter

EXECUTOR = "rebalancer-okf-executor/0.1"
RECEIPT_FIELDS = [
    "computation",
    "computation_sha256",
    "executed_sql",
    "parameters",
    "data_revision",
    "executed_at",
    "executor",
    "result",
    "result_sha256",
]
# Tables a computation may read; each is a Parquet file (or folder) in the data dir.
TABLES = [
    "agg_station_day",
    "agg_station_month",
    "dim_zone",
    "dim_station",
    "fct_recorder_minute",
    "fct_inferred_rebalancing",
]
TYPE_CHECKS = {
    "date": lambda v: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)) and _is_date(v),
    "month": lambda v: bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v)),
    "integer": lambda v: bool(re.fullmatch(r"-?\d+", v)),
    "string": lambda v: isinstance(v, str),
}


class AttestationError(Exception):
    pass


def _is_date(v: str) -> bool:
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def canonical(sql: str) -> str:
    """Comments stripped and whitespace collapsed; nothing else is forgiven."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"\s+", " ", sql).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load(bundle: Path, computation: str) -> tuple[dict, str]:
    """(contract frontmatter, sanctioned SQL) for a computation concept id."""
    path = bundle / f"{computation.strip('/').removesuffix('.md')}.md"
    fm, body, problem = split_frontmatter(path.read_text())
    if problem or not fm or fm.get("type") != "Attested Computation":
        raise AttestationError(f"{computation} is not an Attested Computation")
    if fm.get("runtime") != "duckdb":
        raise AttestationError(f"{computation}: unsupported runtime {fm.get('runtime')!r}")
    if fm.get("computation"):
        sql_path = bundle / str(fm["computation"]).lstrip("/")
        sql = sql_path.read_text()
    else:
        m = re.search(r"# Computation\s+```sql\n(.*?)```", body, re.S)
        if not m:
            raise AttestationError(f"{computation}: no computation found")
        sql = m.group(1)
    return fm, sql


def check_parameters(fm: dict, params: dict[str, str]) -> list[str]:
    declared = {p["name"]: p for p in fm.get("parameters") or []}
    problems = [f"unknown parameter '{k}'" for k in params if k not in declared]
    for name, spec in declared.items():
        if name not in params:
            if spec.get("required", False):
                problems.append(f"missing required parameter '{name}'")
            continue
        check = TYPE_CHECKS.get(spec.get("type", "string"), TYPE_CHECKS["string"])
        if not check(str(params[name])):
            problems.append(f"parameter '{name}'={params[name]!r} is not a valid {spec['type']}")
    return problems


def referenced_tables(sql: str) -> list[str]:
    return [t for t in TABLES if re.search(rf"\b{t}\b", sql)]


def _table_path(data: Path, table: str) -> Path:
    for p in (data / f"{table}.parquet", data / table):
        if p.exists():
            return p
    raise AttestationError(f"data for table {table} not found in {data}")


def data_revision(data: Path, tables: list[str]) -> str:
    """Fingerprint of the exact input files: re-running on other data fails the check."""
    h = hashlib.sha256()
    for t in sorted(tables):
        p = _table_path(data, t)
        files = sorted(p.rglob("*.parquet")) if p.is_dir() else [p]
        for f in files:
            h.update(f"{t}:{f.name}:".encode())
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    # DuckDB sums arrive as DECIMAL or DOUBLE; keep whole numbers whole ("18", not "18.0")
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _bind(fm: dict, params: dict[str, str]) -> dict:
    out = {}
    for p in fm.get("parameters") or []:
        if p["name"] in params:
            v = params[p["name"]]
            out[p["name"]] = int(v) if p.get("type") == "integer" else str(v)
    return out


def execute(sql: str, params: dict, data: Path, tables: list[str]) -> list[dict]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for t in tables:
        p = _table_path(data, t)
        src = (
            f"read_parquet('{p.as_posix()}/**/*.parquet', hive_partitioning = true)"
            if p.is_dir()
            else f"read_parquet('{p.as_posix()}')"
        )
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM {src}")
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, map(_jsonable, r), strict=True)) for r in cur.fetchall()]
    con.close()
    return rows


def result_sha(rows: list[dict]) -> str:
    return sha256(json.dumps(rows, sort_keys=True, default=str))


def run(bundle: Path, computation: str, params: dict[str, str], data: Path) -> dict:
    """Execute a sanctioned computation and return its receipt."""
    fm, sql = load(bundle, computation)
    problems = check_parameters(fm, params)
    if problems:
        raise AttestationError("; ".join(problems))
    tables = referenced_tables(sql)
    bound = _bind(fm, params)
    rows = execute(sql, bound, data, tables)
    return {
        "computation": computation.strip("/").removesuffix(".md"),
        "computation_sha256": sha256(canonical(sql)),
        "executed_sql": sql,
        "parameters": {k: str(v) for k, v in params.items()},
        "data_revision": data_revision(data, tables),
        "executed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "executor": EXECUTOR,
        "result": rows,
        "result_sha256": result_sha(rows),
    }


def attest(
    bundle: Path,
    receipt: dict,
    data: Path | None = None,
    claimed: object | None = None,
) -> dict:
    """Deterministic verdict on a receipt. Callers must not show a value whose
    verdict is not ok."""
    checks: dict[str, bool | None] = {}
    reasons: list[str] = []
    missing = [f for f in RECEIPT_FIELDS if f not in receipt]
    if missing:
        return {"ok": False, "reason": f"receipt is missing {missing}", "checks": {}}
    try:
        fm, sql = load(bundle, receipt["computation"])
    except (AttestationError, FileNotFoundError) as exc:
        return {"ok": False, "reason": str(exc), "checks": {}}

    checks["sanctioned_sql"] = canonical(receipt["executed_sql"]) == canonical(sql)
    checks["computation_fingerprint"] = receipt["computation_sha256"] == sha256(canonical(sql))
    param_problems = check_parameters(fm, receipt["parameters"])
    checks["parameters"] = not param_problems
    checks["result_fingerprint"] = receipt["result_sha256"] == result_sha(receipt["result"])
    if not checks["sanctioned_sql"]:
        reasons.append("the SQL that ran is not the sanctioned computation")
    if not checks["computation_fingerprint"]:
        reasons.append("computation fingerprint doesn't match the sanctioned SQL")
    reasons += param_problems
    if not checks["result_fingerprint"]:
        reasons.append("the result was altered after the run")

    if data is not None and checks["sanctioned_sql"] and checks["parameters"]:
        tables = referenced_tables(sql)
        same_data = data_revision(data, tables) == receipt["data_revision"]
        checks["same_data"] = same_data
        if same_data:
            rerun = execute(sql, _bind(fm, receipt["parameters"]), data, tables)
            checks["reproduces"] = result_sha(rerun) == receipt["result_sha256"]
            if not checks["reproduces"]:
                reasons.append("re-running on the same data gives a different result")
        else:
            checks["reproduces"] = None
            reasons.append("the data has changed since the run; can't reproduce it")

    if claimed is not None:
        values = {str(v) for row in receipt["result"] for v in row.values()}
        checks["claimed_value"] = str(claimed) in values
        if not checks["claimed_value"]:
            reasons.append(f"claimed value {claimed!r} is not in the result")

    stale_after = fm.get("stale_after")
    stale = bool(stale_after) and datetime.now(UTC) >= datetime.fromisoformat(
        str(stale_after).replace("Z", "+00:00")
    )
    ok = all(v is not False for v in checks.values()) and not reasons
    return {
        "ok": ok,
        "reason": "; ".join(reasons) or None,
        "checks": checks,
        "definition_stale": stale,  # §10.6: verification of the definition is separate
    }


def list_computations(bundle: Path) -> list[dict]:
    out = []
    for path in sorted((bundle / "computations").glob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        fm, _, problem = split_frontmatter(path.read_text())
        if fm and not problem and fm.get("type") == "Attested Computation":
            out.append(
                {
                    "id": f"computations/{path.stem}",
                    "title": fm.get("title"),
                    "description": fm.get("description"),
                    "parameters": fm.get("parameters") or [],
                }
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["list", "run"])
    parser.add_argument("computation", nargs="?")
    parser.add_argument("--param", action="append", default=[], help="name=value")
    parser.add_argument("--bundle", type=Path, default=Path("okf/bundle"))
    parser.add_argument("--data", type=Path, default=Path("work/marts"))
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(list_computations(args.bundle), indent=2))
        return
    params = dict(p.split("=", 1) for p in args.param)
    receipt = run(args.bundle, args.computation, params, args.data)
    verdict = attest(args.bundle, receipt, args.data)
    print(json.dumps({"verdict": verdict, "receipt": receipt}, indent=2, default=str))


if __name__ == "__main__":
    main()
