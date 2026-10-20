-- Recording health: one row per UTC minute, with whether any status poll succeeded.
{{ config(location=env_var('MARTS_ROOT') ~ '/fct_recorder_minute.parquet') }}

select * from {{ ref('int_recorder_minute') }}
