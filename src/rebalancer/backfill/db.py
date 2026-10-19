"""DuckDB connections with a memory cap.

DuckDB uses up to 80% of RAM by default, which once froze a laptop running the
backfill. Every connection in this package goes through `connect()` instead.
Override with DUCKDB_MEMORY_LIMIT / DUCKDB_THREADS (the GitHub workflow allows more).
"""

from __future__ import annotations

import os

import duckdb


def connect() -> duckdb.DuckDBPyConnection:
    limit = os.environ.get("DUCKDB_MEMORY_LIMIT", "1GB")
    threads = int(os.environ.get("DUCKDB_THREADS", "2"))
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{limit}'")
    con.execute(f"SET threads = {threads}")
    con.execute("SET preserve_insertion_order = false")  # lets big COPYs stream
    return con
