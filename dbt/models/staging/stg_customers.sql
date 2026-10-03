select
    entity_id as customer_id,
    event_id,
    occurred_at as created_at,
    event_date,
    {{ payload_str('region_id') }} as region_id,
    {{ payload_str('tier') }} as initial_tier,
    {{ payload_str('industry') }} as industry,
    {{ payload_str('preferred_payment_method') }} as preferred_payment_method,
    {{ payload_bool('is_existing') }} as is_existing,
    {{ payload_int('tenure_days') }} as tenure_days_at_start
from {{ ref('stg_sim_events') }}
where event_type = 'customer.created'
