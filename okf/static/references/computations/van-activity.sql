-- Likely Lyft van visits per New York day between two dates (inclusive), optionally
-- within a named zone.
WITH s AS (
    SELECT * FROM agg_station_day
    WHERE local_date BETWEEN CAST($start_date AS DATE) AND CAST($end_date AS DATE)
)
SELECT
    local_date,
    sum(van_drop_offs) AS drop_off_visits,
    sum(van_pick_ups) AS pick_up_visits,
    sum(bikes_dropped_off) AS bikes_dropped_off,
    sum(bikes_picked_up) AS bikes_picked_up,
    count(*) FILTER (WHERE van_drop_offs + van_pick_ups > 0) AS stations_visited
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
GROUP BY local_date
ORDER BY local_date
