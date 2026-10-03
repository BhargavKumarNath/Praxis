{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'observed_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['source', 'metric']
) }}
{% endif %}

select
    record_key,
    observed_date,
    observed_at,
    source,
    series_id,
    entity_id,
    metric,
    unit,
    value,
    batch_id,
    retrieved_at,
    quality_status
from {{ ref('stg_external_signals') }}
