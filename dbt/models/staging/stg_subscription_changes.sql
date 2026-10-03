select
    entity_id as customer_id,
    event_id,
    occurred_at as changed_at,
    event_date,
    {{ payload_str('from_tier') }} as from_tier,
    {{ payload_str('to_tier') }} as to_tier,
    {{ payload_int('base_fee_minor') }} as base_fee_minor
from {{ ref('stg_sim_events') }}
where event_type = 'subscription.changed'
