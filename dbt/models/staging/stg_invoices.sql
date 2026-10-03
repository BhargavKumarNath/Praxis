select
    {{ payload_str('invoice_id') }} as invoice_id,
    entity_id as customer_id,
    event_id,
    occurred_at as invoiced_at,
    event_date,
    {{ payload_int('amount_minor') }} as amount_minor,
    {{ payload_str('currency') }} as currency,
    {{ payload_date('period_start') }} as period_start,
    {{ payload_date('period_end') }} as period_end,
    {{ payload_str('tier') }} as tier
from {{ ref('stg_sim_events') }}
where event_type = 'invoice.created'
