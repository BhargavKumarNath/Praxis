{# BigQuery physical layout: partitioned, partition filter required (cost guard), clustered.
   Guarded because dbt-duckdb reads `partition_by` as an external-table option. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['customer_id']
) }}
{% endif %}

with attempts as (
    select * from {{ ref('stg_payment_events') }} where outcome = 'attempted'
),

resolved as (
    select * from {{ ref('stg_payment_events') }} where outcome in ('succeeded', 'failed')
)

select
    a.invoice_id || '-' || cast(a.attempt_number as {{ type_text() }}) as payment_key,
    a.event_date,
    a.invoice_id,
    a.customer_id,
    a.attempt_number,
    a.amount_minor,
    a.currency,
    a.payment_method,
    coalesce(r.outcome, 'pending') as outcome,
    r.failure_reason,
    coalesce(r.is_final, false) as is_final_failure,
    a.occurred_at as attempted_at,
    r.occurred_at as resolved_at
from attempts as a
left join resolved as r
    on a.invoice_id = r.invoice_id and a.attempt_number = r.attempt_number
