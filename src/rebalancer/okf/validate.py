"""Validate an OKF v0.2 bundle (spec: docs/DESIGN.md, "Context layer for agents").

Errors are the spec's hard conformance rules (§11): every concept file has
parseable frontmatter with a non-empty `type`; reserved files follow §8/§9.
Warnings are the soft guidance a consumer must tolerate (§5-§10): timestamp
formats, actor names, trust and lifecycle fields, computation contracts, links.

    okf-validate path/to/bundle
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md"}
STATUSES = {"draft", "stable", "deprecated"}
ACTOR = re.compile(r"^(human:\S+|process:\S+|[^\s/:]+/\S+)$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
LOG_DATE = re.compile(r"^## (.+?)\s*$", re.M)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    concepts: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def split_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """Returns (frontmatter, body, problem). frontmatter is None when absent/bad."""
    if not text.startswith("---"):
        return None, text, "no frontmatter block"
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text, "frontmatter must open with '---' on its own line"
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None, text, "frontmatter is never closed with '---'"
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        return None, text, f"frontmatter is not valid YAML ({exc.__class__.__name__})"
    if not isinstance(data, dict):
        return None, text, "frontmatter is not a key/value mapping"
    return data, "\n".join(lines[end + 1 :]), None


def _timestamp(value) -> bool:
    """ISO 8601 datetime with an explicit UTC offset (§5)."""
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _check_actor_event(where: str, key: str, event, rep: Report) -> None:
    if not isinstance(event, dict) or "by" not in event:
        rep.warnings.append(f"{where}: {key} entries need a 'by' actor")
        return
    if not ACTOR.match(str(event["by"])):
        rep.warnings.append(f"{where}: {key}.by '{event['by']}' isn't an actor (§7)")
    if "at" in event and not _timestamp(event["at"]):
        rep.warnings.append(f"{where}: {key}.at must be ISO 8601 with a UTC offset")


def check_concept(where: str, fm: dict, body: str, rep: Report) -> None:
    if "generated" in fm:
        _check_actor_event(where, "generated", fm["generated"], rep)
    if "verified" in fm:
        events = fm["verified"] if isinstance(fm["verified"], list) else [fm["verified"]]
        for e in events:
            _check_actor_event(where, "verified", e, rep)
    if "status" in fm and fm["status"] not in STATUSES:
        rep.warnings.append(f"{where}: status '{fm['status']}' isn't draft/stable/deprecated")
    if "stale_after" in fm and not _timestamp(fm["stale_after"]):
        rep.warnings.append(f"{where}: stale_after must be ISO 8601 with a UTC offset")
    for i, src in enumerate(fm.get("sources") or []):
        if not isinstance(src, dict) or not src.get("resource"):
            rep.warnings.append(f"{where}: sources[{i}] needs a resource")
    if fm.get("type") == "Attested Computation":
        if not fm.get("runtime"):
            rep.warnings.append(f"{where}: an Attested Computation needs a runtime (§10.2)")
        for p in fm.get("parameters") or []:
            if not isinstance(p, dict) or not {"name", "type"} <= set(p):
                rep.warnings.append(f"{where}: parameters entries need name and type")
        if not fm.get("computation") and "# Computation" not in body:
            rep.warnings.append(f"{where}: no computation file and no '# Computation' body")


def check_links(root: Path, path: Path, body: str, rep: Report) -> None:
    for target in LINK.findall(body):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue  # external URL or in-page anchor
        target = target.split("#", 1)[0]
        resolved = (root / target.lstrip("/")) if target.startswith("/") else path.parent / target
        if not resolved.exists():
            rep.warnings.append(f"{path.relative_to(root)}: broken link to {target}")


def validate(root: Path) -> Report:
    rep = Report()
    for path in sorted(root.rglob("*.md")):
        where = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        if path.name == "index.md":
            if text.startswith("---"):
                fm, body, problem = split_frontmatter(text)
                at_root = path.parent == root
                if problem or not at_root or set(fm or {}) - {"okf_version"}:
                    rep.errors.append(
                        f"{where}: only the bundle-root index.md may have frontmatter, "
                        "and only okf_version (§8)"
                    )
                text = body
            check_links(root, path, text, rep)
            continue
        if path.name == "log.md":
            for d in LOG_DATE.findall(text):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                    rep.errors.append(f"{where}: log date heading '{d}' isn't YYYY-MM-DD (§9)")
            continue
        fm, body, problem = split_frontmatter(text)
        if problem:
            rep.errors.append(f"{where}: {problem}")
            continue
        if not str(fm.get("type") or "").strip():
            rep.errors.append(f"{where}: frontmatter has no 'type' (the one required field)")
            continue
        rep.concepts += 1
        check_concept(where, fm, body, rep)
        check_links(root, path, body, rep)
    return rep


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    args = parser.parse_args()
    rep = validate(args.bundle)
    for e in rep.errors:
        print(f"ERROR   {e}")
    for w in rep.warnings:
        print(f"warning {w}")
    print(f"{rep.concepts} concepts, {len(rep.errors)} errors, {len(rep.warnings)} warnings")
    if rep.errors or (args.strict and rep.warnings):
        sys.exit(1)


if __name__ == "__main__":
    main()
