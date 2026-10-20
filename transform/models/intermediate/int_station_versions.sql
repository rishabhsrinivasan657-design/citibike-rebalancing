-- Every distinct version of every station, from the station_information log.
-- A version ends when the station changes, or when a later full snapshot no longer
-- lists it (the station was removed).
with info as (
    select
        *,
        md5(concat_ws('|', short_name, name, lat, lon, region_id, capacity, station_type))
            as content_hash
    from {{ ref('stg_station_information') }}
),

snapshots as (
    select distinct polled_at from info
),

changes as (
    select *
    from info
    qualify content_hash is distinct from lag(content_hash) over (
        partition by station_id order by polled_at
    )
),

last_seen as (
    select station_id, max(polled_at) as last_seen_at from info group by 1
),

removed as (
    select l.station_id, min(s.polled_at) as removed_at
    from last_seen as l
    join snapshots as s on s.polled_at > l.last_seen_at
    group by 1
)

select
    c.station_id,
    c.short_name,
    c.name,
    c.lat,
    c.lon,
    c.region_id,
    c.capacity,
    c.station_type,
    c.has_kiosk,
    c.content_hash,
    c.polled_at as valid_from,
    coalesce(
        lead(c.polled_at) over (partition by c.station_id order by c.polled_at),
        r.removed_at
    ) as valid_to
from changes as c
left join removed as r using (station_id)
