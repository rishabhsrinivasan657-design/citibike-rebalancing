-- Likely Lyft van visits (rule v3, see docs/DESIGN.md).
-- A run of 1-3 consecutive recorded minutes where a station's total bikes (available
-- + disabled) move the same way by n bikes is flagged when riders would almost never
-- do that: under a Poisson model with the station's usual departure (or arrival) rate
-- for that local hour and day type, doubled for an unusually busy day, the chance of
-- n or more is below 0.001. Only minutes with the recorder up count.
{{ config(location=env_var('MARTS_ROOT') ~ '/fct_inferred_rebalancing.parquet') }}

{% set busy_day_factor = 2.0 %}
{% set max_p = 0.001 %}
{% set min_bikes = 7 %}  {# groups of friends rarely rent or return more than 6 together #}

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
        v.short_name,
        v.region_id
    from events as e
    asof join {{ ref('int_station_versions') }} as v
        on e.station_id = v.station_id and e.started_minute >= v.valid_from
    -- Jersey City (70) and Hoboken (311) trips are published in separate files we don't
    -- download, so their profiles look nearly empty and every change would be flagged.
    where v.region_id not in ('70', '311')
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
),

flagged as (
    select *, case when bikes_change > 0 then 'drop_off' else 'pick_up' end as action
    from scored
    where p_riders < {{ max_p }}
),

-- A rider in the middle of a van visit splits it into two runs; same-direction
-- flagged runs at a station less than 10 minutes apart are one visit.
visits as (
    select
        *,
        sum(new_visit) over (partition by station_id order by started_minute) as visit_id
    from (
        select
            *,
            case
                when started_minute - lag(ended_minute) over w < interval 10 minute
                    and lag(action) over w = action
                then 0 else 1
            end as new_visit
        from flagged
        window w as (partition by station_id order by started_minute)
    )
)

select
    station_id,
    min(started_minute) as started_minute,
    max(ended_minute) as ended_minute,
    sum(minutes)::integer as minutes,
    sum(bikes_change)::integer as bikes_change,
    any_value(action) as action,
    any_value(day_type) as day_type,
    any_value(local_hour) as local_hour,
    round(sum(expected_riders), 2) as expected_riders,
    min(p_riders) as p_riders,
    count(*) as runs_merged
from visits
group by station_id, visit_id
