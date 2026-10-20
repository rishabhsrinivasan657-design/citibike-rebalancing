select
    station_id as short_name,
    day_type,
    hour as local_hour,
    departures as avg_departures,
    arrivals as avg_arrivals,
    days
from {{ source('history', 'station_hour_profile') }}
