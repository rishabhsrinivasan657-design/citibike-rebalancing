-- Observed weather and what was forecast at the time, side by side, per UTC hour.
select
    o.time_utc as hour_utc,
    o.temperature_2m as temp_c,
    o.apparent_temperature as feels_like_c,
    o.precipitation as precip_mm,
    o.snowfall as snow_cm,
    o.wind_speed_10m as wind_kmh,
    o.weather_code,
    f.temperature_2m as forecast_temp_c,
    f.precipitation as forecast_precip_mm,
    f.snowfall as forecast_snow_cm,
    f.wind_speed_10m as forecast_wind_kmh,
    f.weather_code as forecast_weather_code
from {{ source('history', 'weather_observed') }} as o
left join {{ source('history', 'weather_forecast') }} as f using (time_utc)
