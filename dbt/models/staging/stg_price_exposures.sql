select
    event_id,
    entity_id as customer_id,
    occurred_at,
    event_date,
    {{ payload_str('product') }} as product,
    {{ payload_int('list_price_micros') }} as list_price_micros,
    {{ payload_int('unit_price_micros') }} as unit_price_micros,
    {{ payload_str('experiment_id') }} as experiment_id,
    {{ payload_str('arm') }} as arm
from {{ ref('stg_sim_events') }}
where event_type = 'price.exposed'
