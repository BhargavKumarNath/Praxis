{# Customer-level pricing exposure history (SYNTHETIC; Phase 9): one row per (customer, product)
   price CHANGE the customer was shown, i.e. a price exposure whose unit price differs from the
   same customer's previous exposure to that product. A customer's first exposure (arrival) is
   not a change; an experiment's control-arm exposure at the start (same price) is not either.
   Feeds the optimiser's customer-level price-change frequency guardrail.
   BigQuery physical layout: partitioned (filter required), clustered; guarded as in fct_payments. #}
{% if target.type == 'bigquery' %}
{{ config(
    partition_by={'field': 'event_date', 'data_type': 'date'},
    require_partition_filter=true,
    cluster_by=['customer_id']
) }}
{% endif %}

with ordered as (
    select
        event_id,
        customer_id,
        product,
        occurred_at,
        event_date,
        unit_price_micros,
        experiment_id,
        lag(unit_price_micros) over (
            partition by customer_id, product order by occurred_at, event_id
        ) as previous_price_micros
    from {{ ref('stg_price_exposures') }}
)

select
    event_id as change_id,
    customer_id,
    product,
    occurred_at,
    event_date,
    previous_price_micros,
    unit_price_micros,
    experiment_id
from ordered
where previous_price_micros is not null and unit_price_micros <> previous_price_micros
