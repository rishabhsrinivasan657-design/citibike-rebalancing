-- Every ride once, with New York and UTC times and the GBFS station each end used.
-- Station IDs are matched through `short_name` to the station version current today;
-- stations removed before recording began stay unmatched (start_station_id is null).
{{ config(
    location=env_var('MARTS_ROOT') ~ '/fct_trips',
    options={'partition_by': 'month', 'overwrite_or_ignore': true}
) }}

with stations as (
    select short_name, station_id
    from {{ ref('int_station_versions') }}
    where valid_to is null and short_name is not null
)

select
    t.*,
    s.station_id as start_station_id,
    e.station_id as end_station_id
from {{ ref('stg_trips') }} as t
left join stations as s on s.short_name = t.start_short_name
left join stations as e on e.short_name = t.end_short_name
