-- Station history (SCD2): one row per version, valid from `valid_from` until
-- `valid_to` (null = still current). Built from the log rather than `dbt snapshot`;
-- see docs/DESIGN.md.
{{ config(location=env_var('MARTS_ROOT') ~ '/dim_station.parquet') }}

select
    *,
    valid_to is null as is_current
from {{ ref('int_station_versions') }}
