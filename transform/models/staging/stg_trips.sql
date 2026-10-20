-- One row per ride. Citi Bike lists month-end rides in both months' files; keep the
-- copy from the file of the month the ride started in.
-- Times in the files are New York local time. On the night clocks go back, the
-- repeated 1–2am hour is read as standard time (the later of the two).
with ranked as (
    select
        *,
        row_number() over (
            partition by ride_id
            order by (strftime(started_at, '%Y-%m') = month) desc, month
        ) as copy_number
    from {{ source('history', 'trips') }}
)

select
    ride_id,
    rideable_type,
    member_casual,
    started_at as started_at_local,
    ended_at as ended_at_local,
    timezone('America/New_York', started_at) as started_at_utc,
    timezone('America/New_York', ended_at) as ended_at_utc,
    epoch(ended_at - started_at) / 60.0 as duration_min,
    start_station_id as start_short_name,
    start_station_name,
    end_station_id as end_short_name,
    end_station_name,
    start_lat,
    start_lng,
    end_lat,
    end_lng,
    strftime(started_at, '%Y-%m') as month
from ranked
where copy_number = 1
