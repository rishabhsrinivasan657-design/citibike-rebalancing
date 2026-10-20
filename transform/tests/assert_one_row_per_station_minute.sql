select station_id, minute, count(*) as n
from {{ ref('fct_station_minute') }}
group by 1, 2
having count(*) > 1
