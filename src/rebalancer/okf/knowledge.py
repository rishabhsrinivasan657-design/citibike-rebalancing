"""Read side of the OKF bundle for agents (Step 8.4): search, read, run approved metrics.

The MCP server (server.py) is a thin wrapper around this. There is deliberately no
way to run free-form SQL: numbers come only from Attested Computations, with a
receipt and a deterministic verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rebalancer.okf.attest import AttestationError, attest, list_computations, run
from rebalancer.okf.validate import RESERVED, split_frontmatter


def trust_tier(fm: dict) -> str:
    """Spec §5.3: from the `verified` field only."""
    events = fm.get("verified") or []
    if isinstance(events, dict):
        events = [events]
    if not events:
        return "unverified"
    if any(str(e.get("by", "")).startswith("human:") for e in events):
        return "human-reviewed"
    return "machine-confirmed"


def _as_time(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


@dataclass
class Concept:
    id: str
    fm: dict
    body: str

    def stale(self, now: datetime) -> bool:
        t = _as_time(self.fm.get("stale_after"))
        return t is not None and now >= t

    def summary(self, now: datetime) -> dict:
        return {
            "id": self.id,
            "type": self.fm.get("type"),
            "title": self.fm.get("title", self.id),
            "description": self.fm.get("description", ""),
            "trust": trust_tier(self.fm),
            "status": self.fm.get("status", "stable"),
            "stale": self.stale(now),
        }


class KnowledgeBase:
    def __init__(self, bundle: Path, data: Path, now: datetime | None = None) -> None:
        self.bundle = bundle
        self.data = data
        self._now = now
        self.concepts: dict[str, Concept] = {}
        for path in sorted(bundle.rglob("*.md")):
            if path.name in RESERVED:
                continue
            fm, body, problem = split_frontmatter(path.read_text())
            if fm and not problem and fm.get("type"):
                cid = path.relative_to(bundle).with_suffix("").as_posix()
                self.concepts[cid] = Concept(cid, fm, body)

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def search(self, query: str, type: str | None = None, limit: int = 8) -> list[dict]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        scored = []
        for c in self.concepts.values():
            if type and str(c.fm.get("type", "")).lower() != type.lower():
                continue
            title = str(c.fm.get("title", "")).lower()
            desc = str(c.fm.get("description", "")).lower()
            tags = " ".join(map(str, c.fm.get("tags") or [])).lower()
            body = c.body.lower()
            score = sum(
                3 * title.count(w) + 2 * desc.count(w) + 2 * tags.count(w) + min(body.count(w), 5)
                for w in words
            )
            if score:
                scored.append((-score, c.id))
        return [self.concepts[cid].summary(self.now) for _, cid in sorted(scored)[:limit]]

    def read(self, concept_id: str) -> dict:
        cid = concept_id.strip("/").removesuffix(".md")
        c = self.concepts.get(cid)
        if c is None:
            raise KeyError(f"no concept '{cid}'; use search_concepts to find one")
        out = c.summary(self.now)
        out["stale_after"] = str(c.fm.get("stale_after") or "")
        out["text"] = (self.bundle / f"{cid}.md").read_text()
        return out

    def list_metrics(self) -> list[dict]:
        out = []
        for comp in list_computations(self.bundle):
            c = self.concepts[comp["id"]]
            out.append({**comp, "trust": trust_tier(c.fm), "stale": c.stale(self.now)})
        return out

    def _warnings(self, comp: Concept) -> list[str]:
        warnings = []
        if comp.stale(self.now):
            warnings.append(
                f"The definition of {comp.id} is past its review date "
                f"({comp.fm.get('stale_after')})."
            )
        if trust_tier(comp.fm) == "unverified":
            warnings.append(
                f"The definition of {comp.id} has not been verified by a person or a process yet."
            )
        for src in comp.fm.get("sources") or []:
            sid = str(src.get("resource", "")).strip("/").removesuffix(".md")
            table = self.concepts.get(sid)
            if table and table.stale(self.now):
                warnings.append(
                    f"Data in {sid} is stale (stale after "
                    f"{table.fm.get('stale_after')}); the answer may be out of date."
                )
        return warnings

    def run_metric(self, computation: str, parameters: dict) -> dict:
        cid = computation.strip("/").removesuffix(".md")
        if not cid.startswith("computations/"):
            cid = f"computations/{cid}"
        comp = self.concepts.get(cid)
        if comp is None or comp.fm.get("type") != "Attested Computation":
            return {"ok": False, "error": f"no approved metric '{cid}'; use list_metrics"}
        try:
            receipt = run(self.bundle, cid, {k: str(v) for k, v in parameters.items()}, self.data)
        except AttestationError as exc:
            return {"ok": False, "error": str(exc)}
        verdict = attest(self.bundle, receipt, self.data)
        return {
            "ok": verdict["ok"],
            "computation": cid,
            "title": comp.fm.get("title"),
            "parameters": receipt["parameters"],
            "result": receipt["result"] if verdict["ok"] else None,
            "receipt": {
                "computation_sha256": receipt["computation_sha256"][:16],
                "data_revision": receipt["data_revision"][:16],
                "result_sha256": receipt["result_sha256"][:16],
                "executed_at": receipt["executed_at"],
                "executor": receipt["executor"],
            },
            "verdict": verdict,
            "warnings": self._warnings(comp),
        }
