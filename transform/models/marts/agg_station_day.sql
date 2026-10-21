-- One row per station per New York calendar day: how many recorded minutes the
-- station was empty or full, and how many likely van visits it had. Small enough
-- for an agent to query on a laptop (the minute table grows ~3.6M rows a day).
{{ config(location=env_var('MARTS_ROOT') ~ '/agg_station_day.parquet') }}

with minutes as (
    select
        station_id,
        timezone('America/New_York', minute)::date as local_date,
        count(*) filter (where recorder_up) as minutes_recorded,
        count(*) filter (where recorder_up and is_empty) as empty_minutes,
        count(*) filter (where recorder_up and is_full) as full_minutes,
        count(*) filter (where is_stale) as stale_minutes
    from {{ ref('fct_station_minute') }}
    group by all
),

vans as (
    select
        station_id,
        timezone('America/New_York', started_minute)::date as local_date,
        count(*) filter (where action = 'drop_off') as van_drop_offs,
        count(*) filter (where action = 'pick_up') as van_pick_ups,
        coalesce(sum(bikes_change) filter (where action = 'drop_off'), 0) as bikes_dropped_off,
        coalesce(-sum(bikes_change) filter (where action = 'pick_up'), 0) as bikes_picked_up
    from {{ ref('fct_inferred_rebalancing') }}
    group by all
),

stations as (
    select station_id, short_name, name, lat, lon, region_id, capacity
    from {{ ref('dim_station') }}
    where is_current
)

select
    m.station_id,
    m.local_date,
    s.short_name,
    s.name as station_name,
    s.lat,
    s.lon,
    s.region_id,
    s.capacity,
    m.minutes_recorded,
    m.empty_minutes,
    m.full_minutes,
    m.stale_minutes,
    coalesce(v.van_drop_offs, 0) as van_drop_offs,
    coalesce(v.van_pick_ups, 0) as van_pick_ups,
    coalesce(v.bikes_dropped_off, 0) as bikes_dropped_off,
    coalesce(v.bikes_picked_up, 0) as bikes_picked_up
from minutes as m
left join vans as v using (station_id, local_date)
left join stations as s using (station_id)
