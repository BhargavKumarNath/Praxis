select
    entity_id as customer_id,
    event_id,
    occurred_at as started_at,
    event_date,
    {{ payload_str('tier') }} as tier,
    {{ payload_int('base_fee_minor') }} as base_fee_minor
from {{ ref('stg_sim_events') }}
where event_type = 'subscription.started'
