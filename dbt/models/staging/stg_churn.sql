select
    entity_id as customer_id,
    event_id,
    occurred_at as churned_at,
    event_date,
    {{ payload_str('reason') }} as churn_reason,
    {{ payload_int('tenure_days') }} as tenure_days
from {{ ref('stg_sim_events') }}
where event_type = 'churn.observed'
