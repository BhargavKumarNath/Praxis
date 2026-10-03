select event_id, event_date, occurred_at
from {{ ref('stg_sim_events') }}
where event_date <> cast(occurred_at as date)
