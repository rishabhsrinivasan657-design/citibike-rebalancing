-- Stations with the most rides started plus ended between two months (inclusive,
-- 'YYYY-MM'), optionally within a named zone. From the published trip files.
WITH s AS (
    SELECT * FROM agg_station_month
    WHERE month BETWEEN $start_month AND $end_month AND lat IS NOT NULL
)
SELECT
    station_name,
    sum(rides_started) AS rides_started,
    sum(rides_ended) AS rides_ended,
    sum(rides_started + rides_ended) AS rides_total
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
ORDER BY rides_total DESC, station_name
LIMIT $top_n
