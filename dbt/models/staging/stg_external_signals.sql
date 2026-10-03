select
    s.record_key,
    s.source,
    s.series_id,
    s.entity_id,
    s.metric,
    s.unit,
    s.observed_at,
    s.observed_date,
    s.value,
    s.batch_id,
    s.retrieved_at,
    b.quality_status
from {{ source('raw', 'external_signals') }} as s
inner join {{ source('raw', 'ingest_batches') }} as b using (batch_id)
where b.quality_status <> 'quarantined'
