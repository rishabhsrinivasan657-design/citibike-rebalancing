-- Rides started and ended per station per month, from the trip files. Small summary
-- of fct_trips (45M rows) for agents and dashboards.
{{ config(location=env_var('MARTS_ROOT') ~ '/agg_station_month.parquet') }}

with starts as (
    select start_short_name as short_name, month, count(*) as rides_started
    from {{ ref('fct_trips') }}
    where start_short_name is not null
    group by all
),

ends as (
    select end_short_name as short_name, month, count(*) as rides_ended
    from {{ ref('fct_trips') }}
    where end_short_name is not null
    group by all
),

stations as (
    select short_name, station_id, name, lat, lon, region_id
    from {{ ref('dim_station') }}
    where is_current and short_name is not null
)

select
    coalesce(s.short_name, e.short_name) as short_name,
    coalesce(s.month, e.month) as month,
    st.station_id,
    st.name as station_name,
    st.lat,
    st.lon,
    st.region_id,
    coalesce(s.rides_started, 0) as rides_started,
    coalesce(e.rides_ended, 0) as rides_ended
from starts as s
full outer join ends as e using (short_name, month)
left join stations as st on st.short_name = coalesce(s.short_name, e.short_name)
