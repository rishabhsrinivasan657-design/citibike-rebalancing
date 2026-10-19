"""Station-hour demand profile from the trip history (see docs/DESIGN.md).

For each station (trip-file ID, i.e. GBFS `short_name`), day type and local hour:
average departures and arrivals per day, counting days with zero trips.

    <raw>/profiles/station_hour.parquet
"""

from __future__ import annotations

from pathlib import Path

from rebalancer.backfill import db, trips


def build(raw: Path, start_month: str, end_month: str) -> int:
    first = f"{start_month}-01"
    months = trips.months_between(start_month, end_month)
    y, m = map(int, months[-1].split("-"))
    after_last = f"{y + (m == 12):04d}-{m % 12 + 1:02d}-01"
    glob = (raw / "trips").as_posix() + "/month=*/trips.parquet"
    holidays = (raw / "holidays" / "holidays.parquet").as_posix()
    out_dir = raw / "profiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "station_hour.parquet"

    con = db.connect()
    con.execute(
        f"""
        COPY (
          WITH rides AS (   -- month-end rides are listed in two files; keep one copy
            SELECT DISTINCT ON (ride_id) started_at, ended_at, start_station_id, end_station_id
            FROM read_parquet('{glob}')
            WHERE started_at >= DATE '{first}' AND started_at < DATE '{after_last}'
          ),
          days AS (
            SELECT CAST(d AS DATE) AS d,
                   CASE WHEN dayofweek(d) IN (0, 6)
                          OR CAST(d AS DATE) IN (SELECT date FROM read_parquet('{holidays}'))
                        THEN 'weekend' ELSE 'weekday' END AS day_type
            FROM range(DATE '{first}', DATE '{after_last}', INTERVAL 1 DAY) r(d)
          ),
          n_days AS (SELECT day_type, count(*) AS n FROM days GROUP BY 1),
          events AS (
            SELECT start_station_id AS station_id, started_at AS t, 1 AS dep, 0 AS arr
            FROM rides WHERE start_station_id IS NOT NULL
            UNION ALL
            SELECT end_station_id, ended_at, 0, 1
            FROM rides WHERE end_station_id IS NOT NULL
              AND ended_at >= DATE '{first}' AND ended_at < DATE '{after_last}'
          )
          SELECT e.station_id, days.day_type, hour(e.t) AS hour,
                 sum(e.dep) / any_value(n_days.n) AS departures,
                 sum(e.arr) / any_value(n_days.n) AS arrivals,
                 any_value(n_days.n) AS days
          FROM events e
          JOIN days ON days.d = CAST(e.t AS DATE)
          JOIN n_days USING (day_type)
          GROUP BY ALL
          ORDER BY ALL
        ) TO '{out.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
        """
    )
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
    con.close()
    return rows
