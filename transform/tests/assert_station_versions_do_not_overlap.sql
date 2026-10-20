select *
from {{ ref('dim_station') }}
where valid_to is not null and valid_to <= valid_from
