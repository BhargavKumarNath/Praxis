-- An invoice must never be collected twice.
select invoice_id, count(*) as successes
from {{ ref('fct_payments') }}
where outcome = 'succeeded' and {{ partition_bounds('event_date') }}
group by invoice_id
having count(*) > 1
