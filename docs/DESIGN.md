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

## Historical data (Step 2)

```
s3://tripdata/YYYYMM-citibike-tripdata.zip ──► download ──► unzip (every *.csv inside)
                                                   ──► DuckDB (typed read) ──► data/raw/trips/month=YYYY-MM/trips.parquet
                                                                                + _manifest.json
Open-Meteo archive + historical-forecast ──► data/raw/weather/{observed,forecast}.parquet
holidays package (US, NY) ──► data/raw/holidays/holidays.parquet
```

| Choice | Why |
|---|---|
| Raw layer copies the source faithfully | Times stay naive New York local time as published; time-zone handling happens once, in Step 3 (dbt), where it can be tested. |
| All station IDs and ride IDs read as `VARCHAR` | `7293.10` must not become `7293.1`. |
| DuckDB for CSV → Parquet | Reads multi-GB CSVs quickly with explicit types; we'll use it for dbt in Step 3 anyway. |
| One manifest per month | Records source URL, ETag, size, CSV row count and Parquet row count. A month is skipped on re-run only if the manifest exists and the source ETag is unchanged. |
| Zip deleted after conversion | Keeps disk use to ~2 GB for 12 months instead of ~11 GB. |
| Runs on GitHub Actions, stored on Hugging Face (`citibike-history`) | A laptop run hit macOS's out-of-memory warning (2026-10-19). The workflow pulls what's already in the dataset, skips finished months, and pushes each month as soon as it's converted. |
| DuckDB memory capped (`backfill/db.py`) | DuckDB defaults to 80% of RAM. Capped at 1 GB / 2 threads by default; the workflow raises it to 8 GB on GitHub's 16 GB machines. |
| Window: Oct 2025 – Sep 2026 | The latest 12 published months (Citi Bike publishes with ~2–3 weeks' lag). |
| Weather point: Central Park (40.7829, −73.9654) | Single point is enough for a city-wide signal. Both the ERA5-based archive (what happened) and the historical forecast archive (what was predicted) are stored to avoid training on information we won't have live. Open-Meteo is free for non-commercial use. |
| Station match uses the live `station_information.short_name` | Trip files use `short_name`-style IDs. Stations removed during the year won't match, so the target is 97%, not 100%. |

## Move planner (Step 5)

Two stages: an integer program chooses *what* to move; a scheduler decides *who
does it when* and drops anything that can't arrive in time. An independent checker
re-verifies every limit on the final plan, so an impossible move is never shown.

### Inputs (`planner/model.py`)
For each station *i* in the zone, over a horizon of *H* minutes (default 60):
`b_i` bikes now, `D_i` free docks now (so usable capacity `C_i = b_i + D_i`), and
expected departures `dep_i` and arrivals `arr_i` from the station-hour profile.
Projected end inventory with no moves: `e_i = b_i + arr_i − dep_i`.

Vans: `K` vans (default 3), capacity `Q = 25`, shift `T = H`, starting at the zone centre.

Urgency: the minute station *i* is expected to run empty, `b_i / net_out_rate`, or
full, `D_i / net_in_rate`. Weight `w_i = clip(H / minutes_until_problem, 1, 6)`.

### Stage 1: choose the moves (MIP, PuLP + CBC)

*Updated Oct 20, 2026: trips are one pickup plus up to 4 drop-offs. Variables are
`x_ij` (bikes, continuous, rounded down), `w_ij` (binary: j is a stop on a trip from i)
and `y_i` (integer: trips from donor i), with `Σ_j x_ij ≤ Q·y_i`,
`Σ_j w_ij ≤ 4·y_i`, and van time `Σ y_i (deadhead + load) + Σ w_ij (τ_ij + unload)
+ 2·0.25·Σ x ≤ K·T`. The single-drop formulation below is kept for history.*
Variables per allowed pair (donor *i* → receiver *j*): bikes `x_ij ∈ ℤ≥0`, trips `z_ij ∈ ℤ≥0`.

```
min   Σ_i w_i (short_i + over_i)  +  λ Σ_ij τ_ij z_ij
s.t.  x_ij ≤ Q z_ij                                  van capacity per trip
      Σ_j x_ij ≤ b_i                                 can't take bikes that aren't there
      Σ_i x_ij ≤ D_j                                 can't drop more than free docks
      short_i ≥ β − (e_i + in_i − out_i)             β = 2-bike safety buffer
      over_i  ≥ (e_i + in_i − out_i) − (C_i − β)
      Σ_ij z_ij (τ_ij + handling + deadhead) ≤ K·T   total van time
```
`λ = 0.01` per minute makes driving a tie-breaker: a move happens only if it
reduces a problem. To keep the model small, receivers are the 25 most urgent
stations projected short, donors the 25 with the most to spare (overflowing first);
a pair is allowed only if a van could reach *j* via *i* from the depot before the
receiver's deadline. `deadhead` (driving empty between trips) is approximated by
half the median travel time in the zone; Stage 2 uses real times.

### Stage 2: schedule (`planner/schedule.py`)
Chosen moves are split into trips of ≤ Q bikes, ordered by receiver deadline
(earliest first), and each trip goes to the van that can finish it soonest.
Arrival = van free time + drive to *i* + load + drive to *j* + unload. A trip that
would arrive after the shift ends or more than 10 minutes after the receiver's
deadline is dropped and listed as dropped.

### Travel time (`planner/geo.py`)
Manhattan's avenues run ~29° east of north. Coordinates are converted to metres,
rotated by 29° so streets and avenues align with the axes, and distance is the L1
("city block") distance. Minutes = 2 (park) + metres / 200 (≈ 12 km/h). Loading or
unloading takes 2 minutes + 0.25 minutes per bike.

### Demand profile (`backfill profiles`, runs on GitHub)
From the 12 months of trips: per station, per day type (weekday; weekend/holiday),
per local hour, the average number of departures and arrivals per day, counting
days with zero trips. Month-end duplicate rides are removed first. Stored as
`raw/profiles/station_hour.parquet` in the history dataset (~100k rows).

### Score
"Problem bikes" = Σ (short + over) using the moves actually scheduled, compared
with doing nothing. Weighted by urgency for the objective, reported unweighted.

## Warehouse (Step 3): dbt on DuckDB

```
work/raw/   (history dataset: trips, weather, holidays)  ─┐
work/lake/  (live dataset: station_status, station_information, poll_log) ─┤
                                                           ▼
transform/  dbt project ── staging (views) ── intermediate ── marts (Parquet)
                                                           │
                         pushed to the history dataset as marts/<model>/...
```

| Choice | Why |
|---|---|
| dbt + DuckDB, run on GitHub Actions | Same SQL would run on Snowflake by changing the profile; DuckDB is free and fast on a 16 GB runner. Nothing heavy runs on a laptop. |
| GitHub Actions instead of Airflow | Three scheduled jobs don't justify running an Airflow server. The workflows play the orchestrator role; an Airflow DAG could wrap the same commands later. |
| SCD2 built from the event log, not `dbt snapshot` | `dbt snapshot` needs the previous snapshot table to persist between runs, and GitHub runners are thrown away after each run. The live feed's `station_information` log already holds every version, so `dim_station` derives `valid_from`/`valid_to` with window functions. It's fully rebuildable and gives the same result. |
| Full rebuilds for now | At current sizes a full rebuild takes minutes. `fct_station_minute` grows ~3.6M rows/day; when rebuilds get slow it becomes an incremental model with the DuckDB file persisted between runs. |
| Marts written as Parquet (`materialized: external`) | Steps 4 and 6 read them straight from Hugging Face without dbt. Trips are partitioned by month. |
| Deduplication in staging | Trips: one row per `ride_id` (month-end rides appear in two files). Status: overlapping recorder runs write identical rows, so one row per `(station_id, last_reported, state)`, keeping the earliest poll. |
| Van-move rule (v3, 2026-10-20) | A run of 1–3 consecutive recorded minutes in which a station's total bikes (available + disabled) move the same way. For a loss of *n* bikes, riders would have to take ≥ *n* bikes; for a gain, return ≥ *n*. Using the station's average departures (or arrivals) for that local hour and day type from the Step 2 profile, doubled to allow for an unusually busy day, the chance of that under a Poisson model must be below 0.001, and *n* ≥ 7 (groups of friends rarely rent or return more than 6 together). Stations without a profile are skipped, and so are Jersey City and Hoboken: their trips are published in separate files we don't download, so their profiles look nearly empty. v1 (≥ 6 bikes in ≤ 3 minutes for every station) flagged 139 visits in ~3 hours, several of them plausibly rush-hour riders at busy stations. v2 (*n* ≥ 5, all regions): of 10 hand-checked, 5 clear vans, 4 borderline 5–6-bike changes (possibly groups), 1 wrong (Grove St PATH, Jersey City). Validated against trips once the month's trip file is published. |
| Freshness: warn 30 min, error 60 min on `poll_log` | Uploads happen every 15 minutes, plus run time; tighter limits would alert on normal operation. |

## Backtest (Step 6): replaying recorded mornings

For a zone and window `[t0, t0 + H)` on a recorded day, per station *s* and minute *t*:

| Quantity | Source |
|---|---|
| `b_obs(s,t)` bikes available, `cap(s,t)` = bikes + free docks | `fct_station_minute` |
| `van(s,t)` change during an inferred Lyft visit | minute changes inside `fct_inferred_rebalancing` windows |
| `rider(s,t) = b_obs(s,t) − b_obs(s,t−1) − van(s,t)` | derived |
| hidden departures `ℓ_dep(s,t)` when `b_obs(s,t−1) = 0`; hidden returns `ℓ_arr(s,t)` when no free docks | profile rate / 60 for that station, day type and local hour |

A scenario adds moves `m(s,t)` (pickups negative, at the minute loading finishes;
drop-offs positive, at the minute unloading finishes) and steps forward:

```
want = b(s,t−1) + rider(s,t) − ℓ_dep(s,t) + ℓ_arr(s,t) + m(s,t)
b(s,t) = clip(want, 0, cap(s,t));  missed pickups += max(0, −want);  missed returns += max(0, want − cap)
```

Scenarios: **actual** (`m = van`), **no vans** (`m = 0`), **our plan** (`m` = planner moves,
planned from `b_obs(·,t0)` and the profile only). Consistency check: *actual* with
`ℓ = 0` must reproduce `b_obs` exactly. Fluid (fractional) hidden demand keeps the
replay deterministic.

Known biases, stated in every report: hidden demand uses 12-month averages, not that
day's demand; riders turned away at an empty station may go to a neighbour (not
modelled, which favours rebalancing); van visits under the detection threshold count
as riders.

## Demand model (Step 4)

Target: departures and arrivals per station (trip-file `short_name`) per local hour.
Two `HistGradientBoostingRegressor(loss="poisson")` models (counts are non-negative
and skewed; Poisson loss predicts means directly).

| Item | Choice |
|---|---|
| Training rows | Every station-hour between the station's first and last trip in the window, zeros included (~20M). A random 6M sample trains; validation and test use all rows. |
| Features | profile departures/arrivals for (station, day type, hour); hour; day of week; holiday; month; forecast temperature, precipitation, snowfall, wind (Open-Meteo historical-forecast archive). |
| Split | train Oct 2025–Jul 2026, validate Aug 2026 (early stopping), test Sep 2026, used once. |
| Baseline | the profile itself (12-month averages for station, day type, hour). |
| Metrics | MAE and Poisson deviance per station-hour, overall and 7–10am weekdays. |
| Leakage guard | Only forecast weather; the profile used as a feature is computed from the **training months only** when evaluating. |
| Runs on | GitHub Actions (`train.yml`); model + metrics pushed to `models/` in the history dataset. |
| Live use | The app/planner fetches Open-Meteo's live forecast; backtests use the historical-forecast archive for that day. |

## Context layer for agents: OKF (Step 8, planned)

```
dbt build ─► manifest.json, catalog.json, run_results.json, sources.json
                 │
                 ▼
okf/generate.py ─► okf/ bundle (published to the history dataset with the marts)
   glossary/*.md        hand-written business concepts (station, van visit, ...)
   tables/<model>.md    one per dbt model: columns, tests + latest results,
                        links to parents (lineage) and join partners
   sources/*.md         Citi Bike feed, trip files, Open-Meteo
   metrics/<name>.md    Attested Computations (approved SQL + parameters)
   index.md, log.md     reserved files per the spec
                 │
                 ▼
okf/mcp_server.py ─► tools: search_concepts, read_concept, run_metric
                     (no free-form SQL) → answer + receipt
                 │
                 ▼
okf/eval.py ─► 25 questions, with vs without the bundle → okf/EVAL.md
```

| Choice | Why |
|---|---|
| Generate from dbt artifacts, never hand-maintain table docs | dbt is already the single source of truth for models, tests and lineage; a generator keeps the bundle from drifting. |
| `generated` on everything the generator writes; `verified` only from a reviewed list in the repo | Trust signals mean something only if "verified" requires a human. |
| `stale_after` = table's last build + its freshness error window | Uses the existing freshness SLAs; an agent can tell stale data from fresh. |
| Metrics as parameterised SQL with a content hash | The receipt (query hash, parameters, data revision, time, executor) lets anyone re-run and confirm; a modified query fails the hash check. |
| New small mart `agg_station_day` for metrics | Agent queries must run on a laptop; the minute table grows ~3.6M rows/day. |
| MCP server without a raw SQL tool | Forces answers through approved definitions; the eval's "without" arm gets raw SQL to measure the difference. |
| Spec version pinned in the bundle's `index.md` | v0.x will change; one generator module owns the format. |

Spec status (checked 2026-10-21): v0.1 June 12, 2026; v0.2 July 25, 2026, with
`sources`, `generated`/`verified`, `stale_after`, `status` and Attested Computation,
all optional and backward compatible. Whether Attested Computation is a concept type
or a set of keys (`runtime`, `parameters`, `computation`, `executor`, `attester` in
one implementation) is confirmed in phase 8.0 from the official spec.
