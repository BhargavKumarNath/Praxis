select
    event_id,
    event_type,
    source,
    schema_version,
    entity_id,
    occurred_at,
    published_at,
    event_date,
    trace_id,
    correlation_id,
    causation_id,
    is_synthetic,
    payload,
    batch_id
from {{ source('raw', 'sim_events') }}
