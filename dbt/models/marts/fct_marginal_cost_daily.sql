{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['region_id', 'product']
) }}
{% endif %}

{#
  Daily marginal cost per region and product (micro-GBP per billing unit), from the hourly
  infrastructure reports. The mean is an exact integer sum over the hours divided once, so a
  rebuilt warehouse gives bit-identical values. Consumed by the Phase 6 pricing optimiser.
#}
select
    event_date,
    region_id,
    product,
    count(*) as hours,
    sum(marginal_cost_micros) as cost_micros_sum,
    cast(sum(marginal_cost_micros) as {{ type_float() }}) / count(*) as avg_cost_micros,
    min(marginal_cost_micros) as min_cost_micros,
    max(marginal_cost_micros) as max_cost_micros
from {{ ref('stg_marginal_costs') }}
where marginal_cost_micros is not null
group by event_date, region_id, product
