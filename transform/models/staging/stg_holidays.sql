select date as holiday_date, name as holiday_name
from {{ source('history', 'holidays') }}
