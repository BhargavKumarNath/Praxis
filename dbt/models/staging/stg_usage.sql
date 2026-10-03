select
    event_id,
    entity_id as customer_id,
    occurred_at,
    event_date,
    {{ payload_str('product') }} as product,
    {{ payload_str('region_id') }} as region_id,
    {{ payload_int('units') }} as units,
    {{ payload_int('throttled_units') }} as throttled_units,
    {{ payload_int('unit_price_micros') }} as unit_price_micros
from {{ ref('stg_sim_events') }}
where event_type = 'usage.observed'
