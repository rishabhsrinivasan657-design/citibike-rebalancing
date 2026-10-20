-- Overlapping recorder runs write identical readings; keep the earliest observation of
-- each distinct (station, report time, state).
select
    station_id,
    legacy_id,
    num_bikes_available,
    num_ebikes_available,
    num_bikes_disabled,
    num_docks_available,
    num_docks_disabled,
    is_installed,
    is_renting,
    is_returning,
    last_reported,
    polled_at
from {{ source('live', 'station_status') }}
qualify row_number() over (
    partition by
        station_id, last_reported, num_bikes_available, num_ebikes_available,
        num_bikes_disabled, num_docks_available, num_docks_disabled,
        is_installed, is_renting, is_returning
    order by polled_at
) = 1
