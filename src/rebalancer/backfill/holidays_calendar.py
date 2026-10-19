"""US federal + New York State holidays → <raw>/holidays/holidays.parquet."""

from __future__ import annotations

from pathlib import Path

import holidays
import pyarrow as pa
import pyarrow.parquet as pq


def build(years: list[int]) -> pa.Table:
    cal = holidays.country_holidays("US", subdiv="NY", years=years)
    items = sorted(cal.items())
    return pa.table(
        {
            "date": pa.array([d for d, _ in items], type=pa.date32()),
            "name": pa.array([n for _, n in items], type=pa.string()),
        }
    )


def backfill(raw: Path, years: list[int]) -> int:
    out_dir = raw / "holidays"
    out_dir.mkdir(parents=True, exist_ok=True)
    table = build(years)
    pq.write_table(table, out_dir / "holidays.parquet")
    return table.num_rows
