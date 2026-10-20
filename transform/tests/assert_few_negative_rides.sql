-- Rides that end before they start. Expected only around the night clocks go back,
-- when the files' local times are ambiguous. Fail if more than 0.01% of rides.
{{ config(severity='error', error_if='>4500', warn_if='>0') }}
select ride_id, started_at_local, ended_at_local, duration_min
from {{ ref('stg_trips') }}
where duration_min < 0
