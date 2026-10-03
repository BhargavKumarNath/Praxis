{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['customer_id', 'product']
) }}
{% endif %}

select
    event_date,
    customer_id,
    product,
    region_id,
    sum(units) as units,
    sum(throttled_units) as throttled_units,
    sum(units * unit_price_micros) as usage_value_micros,
    count(*) as observations
from {{ ref('stg_usage') }}
group by event_date, customer_id, product, region_id
