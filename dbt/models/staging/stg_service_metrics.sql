select
    event_id,
    entity_id as region_id,
    occurred_at,
    event_date,
    {{ payload_int('capacity_units') }} as capacity_units,
    {{ payload_float('utilization') }} as utilization,
    {{ payload_float('error_rate') }} as error_rate,
    {{ payload_float('latency_p50_ms') }} as latency_p50_ms,
    {{ payload_float('latency_p95_ms') }} as latency_p95_ms
from {{ ref('stg_sim_events') }}
where event_type = 'service.metric_observed'
