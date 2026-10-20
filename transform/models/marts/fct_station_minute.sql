-- Bikes and docks at every station for every minute we were recording.
-- Each minute shows the latest reading observed by the end of that minute.
-- recorder_up = false means we had no poll that minute (values are carried forward);
-- is_stale = the station hasn't produced a reading for over 20 minutes even though
-- the recorder was up (its 15-minute heartbeat is missing).
{{ config(location=env_var('MARTS_ROOT') ~ '/fct_station_minute.parquet') }}

with readings as (
    select * from {{ ref('stg_station_status') }}
),

grid as (
    select s.station_id, r.minute, r.recorder_up
    from (select distinct station_id from readings) as s
    cross join {{ ref('int_recorder_minute') }} as r
),

filled as (
    select
        g.station_id,
        g.minute,
        g.recorder_up,
        r.num_bikes_available,
        r.num_ebikes_available,
        r.num_bikes_disabled,
        r.num_docks_available,
        r.num_docks_disabled,
        r.is_renting,
        r.is_returning,
        r.last_reported,
        r.polled_at as observed_at
    from grid as g
    asof join readings as r
        on g.station_id = r.station_id
        and g.minute + interval 1 minute > r.polled_at
)

select
    f.*,
    v.capacity,
    epoch(f.minute + interval 1 minute - f.observed_at) / 60.0 as minutes_since_observed,
    f.num_bikes_available = 0 and f.is_renting as is_empty,
    f.num_docks_available = 0 and f.is_returning as is_full,
    f.recorder_up
        and epoch(f.minute + interval 1 minute - f.observed_at) / 60.0 > 20 as is_stale
from filled as f
asof left join {{ ref('int_station_versions') }} as v
    on f.station_id = v.station_id
    and f.minute + interval 1 minute > v.valid_from
