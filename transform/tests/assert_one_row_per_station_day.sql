select station_id, local_date, count(*) as n
from {{ ref('agg_station_day') }}
group by 1, 2
having count(*) > 1
