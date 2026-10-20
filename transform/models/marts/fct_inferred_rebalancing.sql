-- Likely Lyft van visits: a station's total bikes (available + disabled) moving by 6 or
-- more in one direction within at most 3 consecutive recorded minutes. Riders rarely
-- take or dock that many bikes at one station that fast. Only minutes where the
-- recorder was up count, so a recording gap can't look like a van.
{{ config(location=env_var('MARTS_ROOT') ~ '/fct_inferred_rebalancing.parquet') }}

with per_minute as (
    select
        station_id,
        minute,
        recorder_up,
        num_bikes_available + num_bikes_disabled as total_bikes,
        num_bikes_available + num_bikes_disabled
            - lag(num_bikes_available + num_bikes_disabled) over w as delta,
        lag(recorder_up) over w as prev_up
    from {{ ref('fct_station_minute') }}
    window w as (partition by station_id order by minute)
),

moves as (
    select *, sign(delta) as direction
    from per_minute
    where recorder_up and prev_up and delta <> 0
),

runs as (
    -- consecutive minutes moving the same way belong to one run
    select
        *,
        sum(new_run) over (partition by station_id order by minute) as run_id
    from (
        select
            *,
            case
                when lag(minute) over w2 = minute - interval 1 minute
                    and lag(direction) over w2 = direction
                then 0 else 1
            end as new_run
        from moves
        window w2 as (partition by station_id order by minute)
    )
)

select
    station_id,
    min(minute) as started_minute,
    max(minute) as ended_minute,
    count(*) as minutes,
    sum(delta)::integer as bikes_change,
    case when sum(delta) > 0 then 'drop_off' else 'pick_up' end as action
from runs
group by station_id, run_id
having abs(sum(delta)) >= 6 and count(*) <= 3
