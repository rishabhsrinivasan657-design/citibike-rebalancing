"""Monthly Citi Bike trip files: download, convert to Parquet, record a manifest.

Output per month (see docs/DESIGN.md, "Historical data"):
    <raw>/trips/month=YYYY-MM/trips.parquet
    <raw>/trips/month=YYYY-MM/_manifest.json
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rebalancer.backfill import db

log = logging.getLogger(__name__)

BUCKET_URL = "https://s3.amazonaws.com/tripdata/"

# Column types for the 2021+ schema. IDs are text on purpose: "7293.10" != "7293.1".
TRIP_COLUMNS = {
    "ride_id": "VARCHAR",
    "rideable_type": "VARCHAR",
    "started_at": "TIMESTAMP",
    "ended_at": "TIMESTAMP",
    "start_station_name": "VARCHAR",
    "start_station_id": "VARCHAR",
    "end_station_name": "VARCHAR",
    "end_station_id": "VARCHAR",
    "start_lat": "DOUBLE",
    "start_lng": "DOUBLE",
    "end_lat": "DOUBLE",
    "end_lng": "DOUBLE",
    "member_casual": "VARCHAR",
}


@dataclass
class SourceFile:
    key: str
    url: str
    etag: str
    size: int


@dataclass
class Manifest:
    month: str
    source_url: str
    source_etag: str
    source_bytes: int
    csv_files: list[str]
    csv_rows: int
    parquet_rows: int
    converted_at: str


def months_between(start: str, end: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' months."""
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def list_sources(client: httpx.Client) -> dict[str, SourceFile]:
    """Map 'YYYY-MM' → NYC monthly zip, from the bucket listing (names vary slightly)."""
    resp = client.get(BUCKET_URL)
    resp.raise_for_status()
    pattern = re.compile(
        r"<Key>((\d{4})(\d{2})-citibike-tripdata(?:\.csv)?\.zip)</Key>.*?"
        r"<ETag>(.*?)</ETag>.*?<Size>(\d+)</Size>",
        re.S,
    )
    out = {}
    for key, year, month, etag, size in pattern.findall(resp.text):
        out[f"{year}-{month}"] = SourceFile(
            key, BUCKET_URL + key, etag.replace("&quot;", "").strip('"'), int(size)
        )
    return out


def month_dir(raw: Path, month: str) -> Path:
    return raw / "trips" / f"month={month}"


def read_manifest(raw: Path, month: str) -> Manifest | None:
    path = month_dir(raw, month) / "_manifest.json"
    if not path.exists():
        return None
    return Manifest(**json.loads(path.read_text()))


def is_current(raw: Path, month: str, source: SourceFile) -> bool:
    m = read_manifest(raw, month)
    return (
        m is not None
        and m.source_etag == source.etag
        and (month_dir(raw, month) / "trips.parquet").exists()
    )


def download(client: httpx.Client, url: str, dest: Path) -> None:
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)


def extract_csvs(zip_path: Path, out_dir: Path) -> list[Path]:
    """Extract every CSV in the zip, including CSVs inside nested zips."""
    found: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/") or "/._" in name:
                continue
            if name.lower().endswith(".csv"):
                target = out_dir / Path(name).name
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                found.append(target)
            elif name.lower().endswith(".zip"):
                nested = out_dir / Path(name).name
                with zf.open(info) as src, nested.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                inner = out_dir / (nested.stem + "_unzipped")
                inner.mkdir()
                found += extract_csvs(nested, inner)
                nested.unlink()
    return sorted(found)


def count_data_rows(csv_path: Path) -> int:
    """Line count minus the header. Independent of the CSV parser, so it can check it."""
    n = 0
    last = b"\n"
    with csv_path.open("rb") as f:
        while chunk := f.read(1 << 22):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        n += 1  # final line without a trailing newline
    return max(n - 1, 0)


def convert(csvs: list[Path], out_path: Path) -> int:
    """Typed CSV → Parquet with DuckDB. Fails loudly on rows that don't fit the schema."""
    tmp = out_path.with_suffix(".parquet.tmp")
    columns = ", ".join(f"'{k}': '{v}'" for k, v in TRIP_COLUMNS.items())
    files = ", ".join(f"'{p.as_posix()}'" for p in csvs)
    con = db.connect()
    con.execute(
        f"""
        COPY (
            SELECT * FROM read_csv([{files}], header = true, columns = {{{columns}}},
                                   quote = '"', escape = '"')
        ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 500000)
        """
    )
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
    con.close()
    tmp.replace(out_path)
    return rows


def backfill_month(
    client: httpx.Client, raw: Path, month: str, source: SourceFile, work_dir: Path | None = None
) -> Manifest:
    out = month_dir(raw, month)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / source.key
        log.info("%s: downloading %s (%.0f MB)", month, source.key, source.size / 1e6)
        download(client, source.url, zip_path)
        csv_dir = tmp_dir / "csv"
        csv_dir.mkdir()
        csvs = extract_csvs(zip_path, csv_dir)
        zip_path.unlink()
        if not csvs:
            raise RuntimeError(f"{source.key} contains no CSV files")
        csv_rows = sum(count_data_rows(p) for p in csvs)
        log.info("%s: converting %d CSV files (%d rows)", month, len(csvs), csv_rows)
        parquet_rows = convert(csvs, out / "trips.parquet")

    manifest = Manifest(
        month=month,
        source_url=source.url,
        source_etag=source.etag,
        source_bytes=source.size,
        csv_files=[p.name for p in csvs],
        csv_rows=csv_rows,
        parquet_rows=parquet_rows,
        converted_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    (out / "_manifest.json").write_text(json.dumps(asdict(manifest), indent=2))
    log.info("%s: wrote %d rows", month, parquet_rows)
    return manifest
