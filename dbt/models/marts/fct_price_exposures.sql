{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['product']
) }}
{% endif %}

-- Prices shown to customers, with experiment assignment. This is the pre-optimiser pricing
-- fact: decision records (model/policy version, guardrail outcome) arrive with Phases 5-6.
select
    event_id as exposure_id,
    event_date,
    occurred_at,
    customer_id,
    product,
    list_price_micros,
    unit_price_micros,
    experiment_id,
    arm
from {{ ref('stg_price_exposures') }}
