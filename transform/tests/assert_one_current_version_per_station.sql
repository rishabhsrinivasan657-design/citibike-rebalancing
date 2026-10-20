-- Every station has exactly one open version, unless it was removed.
select station_id, count(*) filter (where is_current) as current_versions
from {{ ref('dim_station') }}
group by 1
having count(*) filter (where is_current) > 1
