{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['region_id']
) }}
{% endif %}

select
    event_id as metric_id,
    event_date,
    occurred_at,
    region_id,
    capacity_units,
    utilization,
    error_rate,
    latency_p50_ms,
    latency_p95_ms
from {{ ref('stg_service_metrics') }}
