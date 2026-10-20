select distinct
    station_id,
    short_name,
    name,
    lat,
    lon,
    region_id,
    capacity,
    station_type,
    has_kiosk,
    polled_at
from {{ source('live', 'station_information') }}
