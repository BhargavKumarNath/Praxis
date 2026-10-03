select
    event_id,
    entity_id as customer_id,
    {{ payload_str('invoice_id') }} as invoice_id,
    {{ payload_int('attempt_number') }} as attempt_number,
    case event_type
        when 'payment.attempted' then 'attempted'
        when 'payment.succeeded' then 'succeeded'
        when 'payment.failed' then 'failed'
    end as outcome,
    {{ payload_int('amount_minor') }} as amount_minor,
    {{ payload_str('currency') }} as currency,
    {{ payload_str('payment_method') }} as payment_method,
    {{ payload_str('reason') }} as failure_reason,
    {{ payload_bool('final') }} as is_final,
    occurred_at,
    event_date
from {{ ref('stg_sim_events') }}
where event_type in ('payment.attempted', 'payment.succeeded', 'payment.failed')
