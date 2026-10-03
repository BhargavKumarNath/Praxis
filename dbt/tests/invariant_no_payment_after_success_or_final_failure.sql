-- Once an invoice succeeded or finally failed, no later attempt may exist.
with terminal as (
    select invoice_id, min(attempt_number) as terminal_attempt
    from {{ ref('fct_payments') }}
    where (outcome = 'succeeded' or is_final_failure) and {{ partition_bounds('event_date') }}
    group by invoice_id
)

select p.invoice_id, p.attempt_number, t.terminal_attempt
from {{ ref('fct_payments') }} as p
inner join terminal as t on p.invoice_id = t.invoice_id
where p.attempt_number > t.terminal_attempt and {{ partition_bounds('p.event_date') }}
