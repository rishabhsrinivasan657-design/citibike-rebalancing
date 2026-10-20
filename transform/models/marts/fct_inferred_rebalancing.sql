-- Likely Lyft van visits (rule v2, see docs/DESIGN.md).
-- A run of 1-3 consecutive recorded minutes where a station's total bikes (available
-- + disabled) move the same way by n bikes is flagged when riders would almost never
-- do that: under a Poisson model with the station's usual departure (or arrival) rate
-- for that local hour and day type, doubled for an unusually busy day, the chance of
-- n or more is below 0.001. Only minutes with the recorder up count.
{{ config(location=env_var('MARTS_ROOT') ~ '/fct_inferred_rebalancing.parquet') }}

{% set busy_day_factor = 2.0 %}
{% set max_p = 0.001 %}
{% set min_bikes = 5 %}

with per_minute as (
    select
        station_id,
        minute,
        recorder_up,
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
),

events as (
    select
        station_id,
        min(minute) as started_minute,
        max(minute) as ended_minute,
        count(*) as minutes,
        sum(delta)::integer as bikes_change
    from runs
    group by station_id, run_id
    having count(*) <= 3 and abs(sum(delta)) >= {{ min_bikes }}
),

with_context as (
    select
        e.*,
        timezone('America/New_York', e.started_minute) as started_local,
        v.short_name
    from events as e
    asof join {{ ref('int_station_versions') }} as v
        on e.station_id = v.station_id and e.started_minute >= v.valid_from
),

with_rates as (
    select
        c.*,
        case
            when dayofweek(c.started_local) in (0, 6)
                or c.started_local::date in (select holiday_date from {{ ref('stg_holidays') }})
            then 'weekend' else 'weekday'
        end as day_type,
        hour(c.started_local) as local_hour
    from with_context as c
),

rated as (
    select
        r.*,
        -- riders who'd normally make this change in this many minutes
        r.minutes * case
            when r.bikes_change < 0 then p.avg_departures else p.avg_arrivals
        end / 60.0 as expected_riders
    from with_rates as r
    join {{ ref('stg_station_hour_profile') }} as p using (short_name, day_type, local_hour)
),

scored as (
    select
        *,
        1 - list_sum(list_transform(
            range(abs(bikes_change)),
            i -> exp(
                -{{ busy_day_factor }} * expected_riders
                + i * ln(greatest({{ busy_day_factor }} * expected_riders, 1e-9))
                - lgamma(i + 1)
            )
        )) as p_riders
    from rated
)

select
    station_id,
    started_minute,
    ended_minute,
    minutes,
    bikes_change,
    case when bikes_change > 0 then 'drop_off' else 'pick_up' end as action,
    day_type,
    local_hour,
    round(expected_riders, 2) as expected_riders,
    p_riders
from scored
where p_riders < {{ max_p }}
