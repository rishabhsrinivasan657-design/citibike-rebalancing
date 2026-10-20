-- One row per UTC minute from the first to the last status poll: was the recorder up?
with polls as (
    select date_trunc('minute', polled_at) as minute, count(*) as n_polls
    from {{ ref('stg_poll_log') }}
    where feed = 'station_status' and ok
    group by 1
),

bounds as (
    select min(minute) as first_minute, max(minute) as last_minute from polls
),

minutes as (
    select unnest(range(first_minute, last_minute + interval 1 minute, interval 1 minute))
        as minute
    from bounds
)

select
    m.minute,
    coalesce(p.n_polls, 0) as n_polls,
    p.n_polls is not null as recorder_up
from minutes as m
left join polls as p using (minute)
