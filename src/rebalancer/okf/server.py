"""MCP server over the OKF bundle (Step 8.4). See docs/AGENT.md.

okf-sync                 # download the bundle + small marts (needs HF_TOKEN)
okf-mcp                  # serve over stdio, for Claude Desktop and other MCP clients
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rebalancer.okf.knowledge import KnowledgeBase

CACHE = Path(os.environ.get("OKF_CACHE", Path.home() / ".cache" / "citibike-okf"))
MARTS = ["agg_station_day", "agg_station_month", "dim_zone", "fct_recorder_minute"]

INSTRUCTIONS = """\
You answer questions about Citi Bike in New York (stations, empty and full stations,
Lyft van rebalancing, recorder coverage, rides) using a curated knowledge bundle.

Rules:
1. Get numbers ONLY from run_metric. Never estimate, and never compute a number
   yourself from text you read.
2. Find the right metric with list_metrics or search_concepts, and read its concept
   (read_concept) if the parameters are unclear. Dates are New York calendar dates.
3. Only state a value when run_metric returns ok=true. Cite the computation id and
   the receipt's result fingerprint (result_sha256).
4. Pass on every warning (stale data, unverified definitions) in your answer.
5. If no approved metric answers the question, say you can't answer it reliably
   from the approved metrics, and say what is missing. Do not guess.
"""


def build_server(kb: KnowledgeBase):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("citibike-okf", instructions=INSTRUCTIONS)

    @server.tool()
    def search_concepts(query: str, type: str | None = None) -> list[dict]:
        """Search tables, metrics, computations and glossary terms by keywords.
        Optional type filter, e.g. 'Metric', 'Table', 'Glossary Term'."""
        return kb.search(query, type)

    @server.tool()
    def read_concept(concept_id: str) -> dict:
        """Read one concept (e.g. 'tables/agg_station_day', 'glossary/van-visit'), with its
        trust tier (unverified / machine-confirmed / human-reviewed) and staleness."""
        return kb.read(concept_id)

    @server.tool()
    def list_metrics() -> list[dict]:
        """The approved metrics (Attested Computations) and the parameters each takes."""
        return kb.list_metrics()

    @server.tool()
    def run_metric(computation: str, parameters: dict[str, str]) -> dict:
        """Run an approved metric, e.g. computation='computations/empty-stations' with
        parameters {start_date, end_date, zone, top_n}. Returns the result, a receipt,
        the attester's verdict and warnings. Only report results when ok is true."""
        return kb.run_metric(computation, parameters)

    return server


def sync(dest: Path, with_minutes: bool = False) -> None:
    """Download the bundle and the small marts the metrics read. with_minutes adds the
    large minute table and station history (for the trap analysis; use on GitHub)."""
    from huggingface_hub import HfApi, snapshot_download

    from rebalancer.ingest.recorder import resolve_repo_id

    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN must be set (a read-only token is enough)")
    repo = resolve_repo_id(
        HfApi(token=token), os.environ.get("HF_HISTORY_REPO", "citibike-history"), False
    )
    snapshot_download(
        repo,
        repo_type="dataset",
        token=token,
        local_dir=dest,
        allow_patterns=[
            "okf/**",
            *[f"marts/{m}.parquet" for m in MARTS],
            *(
                ["marts/fct_station_minute.parquet", "marts/dim_station.parquet"]
                if with_minutes
                else []
            ),
        ],
    )
    print(f"bundle and marts saved to {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=CACHE / "okf")
    parser.add_argument("--data", type=Path, default=CACHE / "marts")
    args = parser.parse_args()
    if not args.bundle.exists():
        sync(CACHE)
    build_server(KnowledgeBase(args.bundle, args.data)).run("stdio")


def main_sync() -> None:
    parser = argparse.ArgumentParser(description="Download the OKF bundle and small marts.")
    parser.add_argument("--dest", type=Path, default=CACHE)
    parser.add_argument(
        "--with-minutes",
        action="store_true",
        help="also the large minute table (hundreds of MB; for GitHub)",
    )
    args = parser.parse_args()
    sync(args.dest, args.with_minutes)


if __name__ == "__main__":
    main()
