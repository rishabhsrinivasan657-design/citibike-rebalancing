# Design

## Architecture

```
                     every 60 s                       batches (≤5 min)
 GBFS feeds ──► collector ──► Redpanda topics ──► lake-sink ──► Parquet lake
 (Lyft)         (producer)    gbfs.station_status             data/lake/<table>/date=/hour=
                              gbfs.station_information                │
                              gbfs.poll_log                           ▼
                                                              dbt (DuckDB / Snowflake)
 Trip CSVs, weather, holidays ── Airflow backfills ─────────►  staging → marts
                                                                       │
                                               forecast (sklearn) ◄────┤
                                               optimizer (MIP)   ◄─────┤
                                               simulator/backtest ◄────┘
```

## Data sources

| Source | URL | Cadence |
|---|---|---|
| GBFS discovery | `https://gbfs.citibikenyc.com/gbfs/gbfs.json` | resolved at startup |
| Station status | resolved from discovery (currently `gbfs.lyft.com/gbfs/1.1/bkn/en/...`) | 60 s (`ttl` = 60) |
| Station information | resolved from discovery | hourly |
| Trip history | `https://s3.amazonaws.com/tripdata/` | monthly, ~1 month lag |
| Weather | Open-Meteo archive + historical forecast API | hourly |

Observed feed facts (2026-10-19): GBFS v1.1, ~2,500 stations, regions
`71` NYC, `70` Jersey City, `311` Hoboken, `185` Bronx, plus `158` 8D, `189` IC HQ,
`190` testzone (non-public; filtered downstream). Some stations report
`last_reported` far in the past or 0 — these are kept raw and flagged in dbt.

## Data contracts

All timestamps are stored in UTC; conversion to `America/New_York` happens in dbt.

### `gbfs.station_status` (key: `station_id`)
One message per station when it is first seen, when any inventory or operating-state
field changes, or as a heartbeat every 15 min (`STATUS_HEARTBEAT_S`). Inventory is
forward-filled between messages downstream.

| field | type | notes |
|---|---|---|
| station_id | string | GBFS UUID |
| legacy_id | string | |
| num_bikes_available | int | includes e-bikes |
| num_ebikes_available | int | |
| num_bikes_disabled | int | |
| num_docks_available | int | |
| num_docks_disabled | int | |
| is_installed / is_renting / is_returning | bool | |
| last_reported | timestamp | station-side report time |
| feed_last_updated | timestamp | feed-level `last_updated` |
| polled_at | timestamp | collector clock |

### `gbfs.station_information` (key: `station_id`)
Full snapshot published when the feed's content hash changes, and at least once
every 24 h. Fields: `station_id, short_name, name, lat, lon, region_id, capacity,
station_type, has_kiosk, feed_last_updated, polled_at`. `short_name` is the join key
to trip data's `start_station_id` / `end_station_id`.

### `gbfs.poll_log`
One message per poll attempt, success or failure: `polled_at, feed, ok,
http_status, latency_ms, feed_last_updated, n_stations, n_changed, error`.
This is the source for freshness SLAs and soak-test completeness.

## Delivery semantics

- **At-least-once.** The sink commits Kafka offsets only after the Parquet file is
  durably written. A crash between write and commit produces duplicates, never gaps.
- **Dedup downstream.** dbt deduplicates on `(station_id, last_reported, polled_at)`.
- **Collector restart** loses its in-memory change state, so the first poll after a
  restart republishes every station. That is intentional — it doubles as a full
  snapshot.

## Lake layout

```
data/lake/<table>/date=YYYY-MM-DD/hour=HH/part-<unix_ms>-<uuid8>.parquet
```
Partitioned by `polled_at` (UTC). The root is a URI, so `s3://bucket/lake` works
unchanged via `pyarrow.fs`.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Local-first: Docker Compose, DuckDB, local lake; S3/Snowflake as swappable targets | Free, reproducible by any reviewer; the Snowflake trial is only 30 days |
| D2 | Resolve feed URLs from the discovery file | Citi Bike's endpoints already moved to `gbfs.lyft.com` once |
| D3 | CDC on inventory/state fields + 15 min heartbeat | Measured 2026-10-19: ~1,650 of 2,519 stations re-report every minute, but only 21% of re-reports change inventory. Keying on `last_reported` kept ~80% redundant rows; the heartbeat preserves "station went silent" detection |
| D4 | Redpanda even though volume is low | Decouples polling from storage; a sink outage doesn't drop polls. Stated honestly as a design-practice choice, not a scale need |
| D5 | Trip data 2021+ only | Pre-2021 files use a different schema and station IDs |
| D6 | Travel time = rotated-grid L1 distance / van speed (M5) | Manhattan's grid is rotated ~29°; OSRM only if time allows |

## Running 24/7 for free: GitHub Actions + Hugging Face

The collector needs an always-on machine. We use free GitHub Actions runs instead
of a server, and a Hugging Face dataset repo for storage.

```
GitHub Actions (new run every 2 h, each lasts 2 h 45 m)
  └─ recorder: poll every 60 s → Parquet files in ./lake-pending
                 └─ every 15 min: upload to Hugging Face dataset, then delete locally
```

| Choice | Why |
|---|---|
| Runs overlap by 45 min | GitHub often starts scheduled runs late; overlap prevents gaps. Duplicate rows from the overlap are removed downstream. |
| Cron at minute 17, not :00 | GitHub's scheduler is busiest on the hour, which is when delays are worst. |
| No Kafka in the cloud | A run can't keep a broker alive. The `recorder` uses the same `Collector` code but writes Parquet directly. The Kafka path stays as the local demo (`make up`). |
| Upload every 15 min | ~200 commits/day across overlapping runs, well within Hugging Face limits. Files are compacted into one per day in Step 3 to keep the file count low. |
| Failed uploads | Files stay on disk and are retried at the next upload; if the run ends with files still pending, the workflow saves them as a GitHub artifact (kept 7 days) so they can be recovered by hand. |
| Public GitHub repo | Actions minutes are unlimited only for public repos (~2,000 min/day are needed). No secrets live in the code. |
| Keep-alive | GitHub pauses scheduled workflows after 60 days with no repo activity; each run re-enables its own workflow through the API. |
