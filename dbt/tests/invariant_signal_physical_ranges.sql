select record_key, source, metric, value
from {{ ref('fct_external_signals') }}
where observed_date between date '1990-01-01' and date '2100-01-01'
    and (
        (metric in ('relative_humidity_2m', 'cloud_cover') and (value < 0 or value > 100))
        or (metric = 'temperature_2m' and (value < -90 or value > 60))
        or (metric in ('wind_speed_10m', 'precipitation') and value < 0)
        or (metric like 'carbon_intensity_%' and value < 0)
        or (metric = 'electricity_demand' and value < 0)
    )
