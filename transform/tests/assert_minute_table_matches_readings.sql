-- At the minute each reading was observed, the minute table must show that reading
-- (the last one observed in that minute, if there were several).
with last_in_minute as (
    select *
    from {{ ref('stg_station_status') }}
    qualify row_number() over (
        partition by station_id, date_trunc('minute', polled_at) order by polled_at desc
    ) = 1
)

select r.station_id, r.polled_at, r.num_bikes_available, m.num_bikes_available as in_table
from last_in_minute as r
join {{ ref('fct_station_minute') }} as m
    on m.station_id = r.station_id and m.minute = date_trunc('minute', r.polled_at)
where m.num_bikes_available <> r.num_bikes_available
   or m.num_docks_available <> r.num_docks_available
