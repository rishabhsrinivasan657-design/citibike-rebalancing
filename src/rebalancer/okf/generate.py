"""Generate an OKF v0.2 bundle from dbt artifacts (Step 8.2; see docs/DESIGN.md).

    okf-generate --target transform/target --static okf/static --out okf/bundle

Reads manifest.json (models, columns, tests, lineage, meta), run_results.json
(latest test results), and, when present, catalog.json (column types) and
sources.json (freshness). Hand-written concepts in --static (glossary, metrics,
computations) are copied in unchanged. Human reviews come from okf/verified.yml.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

GENERATOR = "okf-generate/0.1"
OKF_VERSION = "0.2"
LAYERS = {"marts": "Mart", "intermediate": "Intermediate table", "staging": "Staging view"}
# A table fed by the live recorder goes stale soon after the warehouse's 6-hourly
# rebuild is missed; one built only from monthly trip files lasts until next month.
LIVE_STALE = timedelta(hours=7)
HISTORY_STALE = timedelta(days=35)
HF_HISTORY = "hf://datasets/RokuRocks/citibike-history"


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


@dataclass
class Artifacts:
    manifest: dict
    run_results: dict
    catalog: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    @classmethod
    def load(cls, target: Path) -> Artifacts:
        def read(name: str) -> dict:
            p = target / name
            return json.loads(p.read_text()) if p.exists() else {}

        # dbt overwrites run_results.json on every command (docs, freshness), so the
        # workflow saves the build's copy as build_results.json right after `dbt build`.
        return cls(
            read("manifest.json"),
            read("build_results.json") or read("run_results.json"),
            read("catalog.json"),
            read("sources.json"),
        )

    @property
    def built_at(self) -> datetime:
        stamp = self.run_results.get("metadata", {}).get("generated_at")
        return parse_time(stamp) if stamp else datetime.now(UTC)

    def models(self) -> dict[str, dict]:
        """dbt models and seeds (small reference tables loaded from CSV)."""
        return {
            k: v
            for k, v in self.manifest["nodes"].items()
            if v["resource_type"] in ("model", "seed")
        }

    def tests_for(self, unique_id: str) -> list[dict]:
        return [
            n
            for n in self.manifest["nodes"].values()
            if n["resource_type"] == "test"
            and (
                n.get("attached_node") == unique_id
                or (
                    not n.get("attached_node")
                    and unique_id in n.get("depends_on", {}).get("nodes", [])
                )
            )
        ]

    def result(self, unique_id: str) -> dict | None:
        return next(
            (r for r in self.run_results.get("results", []) if r["unique_id"] == unique_id), None
        )


def meta(node: dict) -> dict:
    return node.get("meta") or node.get("config", {}).get("meta") or {}


def layer(node: dict) -> str:
    if node["resource_type"] == "seed":
        return "seed"
    parts = Path(node["original_file_path"]).parts
    return next((p for p in parts if p in LAYERS), "staging")


def concept_path(unique_id: str, art: Artifacts) -> str:
    """Bundle-relative path of the concept for a dbt node or source."""
    if unique_id.startswith("source."):
        src = art.manifest["sources"][unique_id]
        return f"/sources/{src['source_name']}__{src['name']}.md"
    return f"/tables/{unique_id.split('.')[-1]}.md"


def link(unique_id: str, art: Artifacts) -> str:
    name = (
        unique_id.split(".")[-1]
        if not unique_id.startswith("source.")
        else ("{source_name}.{name}".format(**art.manifest["sources"][unique_id]))
    )
    return f"[{name}]({concept_path(unique_id, art)})"


def upstream_sources(unique_id: str, art: Artifacts, seen: set | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    node = art.manifest["nodes"].get(unique_id) or {}
    out = set()
    for dep in node.get("depends_on", {}).get("nodes", []):
        if dep in seen:
            continue
        seen.add(dep)
        out |= {dep} if dep.startswith("source.") else upstream_sources(dep, art, seen)
    return out


def frontmatter(data: dict) -> str:
    def flow(v):  # keep {by, at} events on one line, like the spec's examples
        return "{ " + ", ".join(f"{k}: {v2}" for k, v2 in v.items()) + " }"

    lines = ["---"]
    for k, v in data.items():
        if v is None or v == [] or v == {}:
            continue
        if k in ("generated",):
            lines.append(f"{k}: {flow(v)}")
        elif k == "verified":
            lines.append(f"{k}:")
            lines += [f"  - {flow(e)}" for e in v]
        else:
            dumped = yaml.safe_dump({k: v}, sort_keys=False, allow_unicode=True, width=100)
            lines.append(dumped.rstrip("\n"))
    lines.append("---")
    return "\n".join(lines) + "\n"


def model_concept(uid: str, node: dict, art: Artifacts, verified: dict) -> tuple[str, str]:
    m = meta(node)
    name = node["name"]
    built = art.built_at
    tests = art.tests_for(uid)
    results = [(t, art.result(t["unique_id"])) for t in tests]
    all_passed = bool(results) and all(r and r["status"] == "pass" for _, r in results)

    live = any(s.split(".")[-2] == "live" for s in upstream_sources(uid, art))
    if m.get("stale_after_days"):  # a model can state its own refresh window
        stale_after = built + timedelta(days=float(m["stale_after_days"]))
    else:
        stale_after = built + (LIVE_STALE if live else HISTORY_STALE)
    model_result = art.result(uid)
    status = "stable" if (model_result or {}).get("status") in (None, "success") else "draft"

    ver = []
    if all_passed:
        ver.append({"by": "process:dbt-tests", "at": iso(built)})
    ver += [{"by": e["by"], "at": e["at"]} for e in verified.get(f"tables/{name}", [])]

    parents = node.get("depends_on", {}).get("nodes", [])
    fm = {
        "type": "Table",
        "title": name,
        "description": first_sentence(node["description"]),
        "resource": (f"{HF_HISTORY}/marts/{name}" + ("" if name == "fct_trips" else ".parquet"))
        if layer(node) == "marts"
        else f"dbt://citibike/{node['unique_id']}",
        "tags": [layer(node), *node.get("tags", [])],
        "generated": {"by": GENERATOR, "at": iso(built)},
        "verified": ver,
        "status": status,
        "stale_after": iso(stale_after),
        "sources": [
            {"id": p.split(".")[-1], "resource": concept_path(p, art), "title": p.split(".")[-1]}
            for p in parents
        ],
        "grain": m.get("grain"),
    }

    body = [f"# Description\n\n{' '.join(node['description'].split())}\n"]
    if m.get("grain"):
        body.append(f"# Grain\n\nOne row = {m['grain']}.\n")
    cat_cols = art.catalog.get("nodes", {}).get(uid, {}).get("columns", {})
    cols = node.get("columns", {})
    if cols or cat_cols:
        order = list(cat_cols) or list(cols)
        rows = ["| Column | Type | Description |", "|---|---|---|"]
        for c in order:
            key = c if c in cols else c.lower()
            desc = " ".join((cols.get(key, {}).get("description") or "").split())
            ctype = cat_cols.get(c, {}).get("type", cols.get(key, {}).get("data_type") or "")
            rows.append(f"| `{c.lower()}` | {ctype} | {desc.replace('|', '/')} |")
        body.append("# Schema\n\n" + "\n".join(rows) + "\n")
    if m.get("joins"):
        lines = []
        for j in m["joins"]:
            target = f"[{j['to']}](/tables/{j['to']}.md)"
            note = f" ({j['note']})" if j.get("note") else ""
            lines.append(f"- Joins {target} using `{j['using']}`{note}.")
        body.append("# Joins\n\n" + "\n".join(lines) + "\n")
    if parents:
        body.append("# Lineage\n\nBuilt from: " + ", ".join(link(p, art) for p in parents) + ".\n")
    if results:
        rows = ["| Test | Column | Latest result |", "|---|---|---|"]
        for t, r in results:
            rows.append(
                f"| {t.get('test_metadata', {}).get('name', t['name'])} "
                f"| {t.get('column_name') or '–'} | {(r or {}).get('status', 'not run')} |"
            )
        body.append("# Tests\n\n" + "\n".join(rows) + "\n")
    if m.get("caveats"):
        body.append("# Caveats\n\n" + "\n".join(f"- {c}" for c in m["caveats"]) + "\n")
    return f"tables/{name}.md", frontmatter(fm) + "\n" + "\n".join(body)


def source_concept(uid: str, src: dict, art: Artifacts) -> tuple[str, str]:
    m = meta(src)
    fresh = next((r for r in art.sources.get("results", []) if r["unique_id"] == uid), None)
    fm = {
        "type": "Data Source",
        "title": f"{src['source_name']}.{src['name']}",
        "description": first_sentence(src.get("description") or src.get("source_description", "")),
        "resource": m.get("origin") if str(m.get("origin", "")).startswith("http") else None,
        "tags": ["source", src["source_name"]],
        "generated": {"by": GENERATOR, "at": iso(art.built_at)},
    }
    body = [f"# Description\n\n{' '.join((src.get('description') or '').split())}\n"]
    if m.get("origin"):
        body.append(f"# Origin\n\n{m['origin']}\n")
    if m.get("refresh"):
        body.append(f"# Refresh\n\n{m['refresh']}\n")
    if fresh and fresh.get("max_loaded_at"):
        err = (fresh.get("criteria") or {}).get("error_after") or {}
        if err.get("count") and err.get("period"):
            window = timedelta(**{f"{err['period']}s": err["count"]})
            fm["stale_after"] = iso(parse_time(fresh["max_loaded_at"]) + window)
        fm["verified"] = (
            [{"by": "process:dbt-source-freshness", "at": iso(parse_time(fresh["snapshotted_at"]))}]
            if fresh.get("status") == "pass"
            else []
        )
        body.append(
            f"# Freshness\n\nNewest data: {fresh['max_loaded_at']} (checked "
            f"{fresh['snapshotted_at']}; status **{fresh.get('status')}**).\n"
        )
    users = sorted(
        n["name"]
        for n in art.manifest["nodes"].values()
        if uid in n.get("depends_on", {}).get("nodes", [])
    )
    if users:
        body.append("# Used by\n\n" + ", ".join(f"[{u}](/tables/{u}.md)" for u in users) + ".\n")
    return f"sources/{src['source_name']}__{src['name']}.md", frontmatter(fm) + "\n" + "\n".join(
        body
    )


def read_frontmatter(path: Path) -> dict:
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    return yaml.safe_load(text.split("---", 2)[1]) or {}


def write_indexes(out: Path) -> None:
    sections = {
        "tables": "Tables in the warehouse (dbt models): marts first.",
        "sources": "Where the data comes from.",
        "glossary": "Business terms and known data quirks.",
        "metrics": "Business definitions of the headline numbers.",
        "computations": "Sanctioned queries (Attested Computations) for each metric.",
        "references": "Run instructions and attester code used by the computations.",
    }
    root = [
        "---",
        f'okf_version: "{OKF_VERSION}"',
        "---",
        "",
        "# Citi Bike rebalancing knowledge bundle",
        "",
        "Context for agents answering questions about Citi Bike stations, rides, and "
        "rebalancing in New York. Generated from the project's dbt warehouse.",
        "",
    ]
    for d, desc in sections.items():
        folder = out / d
        if not folder.exists():
            continue
        root.append(f"* [{d}]({d}/index.md) - {desc}")
        entries = []
        for f in sorted(folder.rglob("*.md")):
            if f.name in ("index.md", "log.md"):
                continue
            fm = read_frontmatter(f)
            rel = f.relative_to(folder).as_posix()
            tags = fm.get("tags") or []
            order = (0 if "marts" in tags else 1, rel)
            entries.append(
                (order, f"* [{fm.get('title', f.stem)}]({rel}) - {fm.get('description', '')}")
            )
        lines = [f"# {d.capitalize()}", ""] + [e for _, e in sorted(entries)]
        (folder / "index.md").write_text("\n".join(lines) + "\n")
    (out / "index.md").write_text("\n".join(root) + "\n")


def write_log(out: Path, art: Artifacts, previous: Path | None, counts: dict) -> None:
    day = art.built_at.strftime("%Y-%m-%d")
    entry = (
        f"## {day}\n* **Update**: Regenerated from dbt run "
        f"`{art.run_results.get('metadata', {}).get('invocation_id', 'unknown')}`: "
        f"{counts['tables']} tables ({counts['passing']} with every test passing), "
        f"{counts['sources']} sources.\n"
    )
    old = previous.read_text() if previous and previous.exists() else "# Bundle Update Log\n"
    head, _, rest = old.partition("\n")
    (out / "log.md").write_text(f"{head}\n\n{entry}{rest}")


def generate(
    target: Path,
    static: Path | None,
    out: Path,
    verified_file: Path | None = None,
    previous_log: Path | None = None,
) -> dict:
    art = Artifacts.load(target)
    verified = (
        yaml.safe_load(verified_file.read_text())
        if verified_file and verified_file.exists()
        else {}
    )
    verified = verified or {}
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if static and static.exists():
        shutil.copytree(static, out, dirs_exist_ok=True)

    counts = {"tables": 0, "passing": 0, "sources": 0}
    for uid, node in art.models().items():
        rel, text = model_concept(uid, node, art, verified)
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
        counts["tables"] += 1
        counts["passing"] += "process:dbt-tests" in text
    for uid, src in art.manifest.get("sources", {}).items():
        rel, text = source_concept(uid, src, art)
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
        counts["sources"] += 1
    write_indexes(out)
    write_log(out, art, previous_log, counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path("transform/target"))
    parser.add_argument("--static", type=Path, default=Path("okf/static"))
    parser.add_argument("--verified", type=Path, default=Path("okf/verified.yml"))
    parser.add_argument("--previous-log", type=Path)
    parser.add_argument("--out", type=Path, default=Path("okf/bundle"))
    args = parser.parse_args()
    counts = generate(args.target, args.static, args.out, args.verified, args.previous_log)
    print(f"wrote {counts['tables']} tables and {counts['sources']} sources to {args.out}")


if __name__ == "__main__":
    main()
