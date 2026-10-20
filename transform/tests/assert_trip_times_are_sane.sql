-- Rides ending before they start, or lasting over a day, should be rare.
{{ config(severity='error', error_if='>1000', warn_if='>0') }}
select ride_id, started_at_local, ended_at_local, duration_min
from {{ ref('stg_trips') }}
where duration_min < 0 or duration_min > 24 * 60
