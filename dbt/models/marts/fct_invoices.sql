{# One row per invoice (SYNTHETIC): the Phase 8 recovery models need the invoice's creation time,
   amount and tier as billed, independent of how its attempts went.
   BigQuery physical layout: partitioned (filter required), clustered; guarded as in fct_payments. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['customer_id']
) }}
{% endif %}

select
    invoice_id,
    customer_id,
    invoiced_at,
    event_date,
    amount_minor,
    currency,
    period_start,
    period_end,
    tier
from {{ ref('stg_invoices') }}
