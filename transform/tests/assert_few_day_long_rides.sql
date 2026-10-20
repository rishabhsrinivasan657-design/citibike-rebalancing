-- Rides over 24 hours: real in Citi Bike data (bikes not returned properly are closed
-- out later), so this only warns. Steps 4 and 6 leave them out of demand estimates.
{{ config(severity='warn') }}
select ride_id, started_at_local, ended_at_local, duration_min
from {{ ref('stg_trips') }}
where duration_min > 24 * 60
