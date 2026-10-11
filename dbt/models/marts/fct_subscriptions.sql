{# One row per subscription start or tier change (SYNTHETIC). The tier and fee in force at any
   time T are those of the latest row with effective_at < T: the point-in-time view the Phase 9
   customer-value models need (dim_customer.current_tier is the tier at the END of the data and
   must never be used as a feature).
   BigQuery physical layout: partitioned (filter required), clustered; guarded as in fct_payments. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['customer_id']
) }}
{% endif %}

select
    event_id,
    customer_id,
    started_at as effective_at,
    event_date,
    'started' as kind,
    tier,
    base_fee_minor
from {{ ref('stg_subscription_starts') }}
union all
select
    event_id,
    customer_id,
    changed_at as effective_at,
    event_date,
    'changed' as kind,
    to_tier as tier,
    base_fee_minor
from {{ ref('stg_subscription_changes') }}
