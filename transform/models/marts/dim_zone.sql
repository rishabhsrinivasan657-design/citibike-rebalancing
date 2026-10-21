-- Named areas used by the planner, the app and the approved metrics, so "Midtown"
-- means the same circle everywhere.
{{ config(location=env_var('MARTS_ROOT') ~ '/dim_zone.parquet') }}

select zone, center_lat, center_lon, radius_m, description
from {{ ref('zones') }}
