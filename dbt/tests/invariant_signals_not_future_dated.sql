-- A source cannot report an observation later than the moment we retrieved it.
select record_key, source, observed_at, retrieved_at
from {{ ref('fct_external_signals') }}
where observed_date between date '1990-01-01' and date '2100-01-01'
    and observed_at > retrieved_at
