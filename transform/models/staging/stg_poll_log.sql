select distinct
    polled_at,
    feed,
    ok,
    http_status,
    latency_ms,
    feed_last_updated,
    n_stations,
    n_changed,
    error
from {{ source('live', 'poll_log') }}
