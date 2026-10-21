-- Stations that were full (no free dock) for the most recorded minutes between
-- two New York dates (inclusive), optionally within a named zone.
WITH s AS (
    SELECT * FROM agg_station_day
    WHERE local_date BETWEEN CAST($start_date AS DATE) AND CAST($end_date AS DATE)
)
SELECT
    station_name,
    sum(full_minutes) AS full_minutes,
    sum(minutes_recorded) AS minutes_recorded,
    round(sum(full_minutes) / nullif(sum(minutes_recorded), 0), 3) AS full_share
FROM s
WHERE
    $zone = 'all'
    OR EXISTS (
        SELECT 1 FROM dim_zone AS z
        WHERE z.zone = lower($zone)
          AND 6371000 * sqrt(
                pow(radians(s.lat - z.center_lat), 2)
                + pow(radians(s.lon - z.center_lon) * cos(radians(z.center_lat)), 2)
              ) <= z.radius_m
    )
GROUP BY station_name
ORDER BY full_minutes DESC, station_name
LIMIT $top_n
