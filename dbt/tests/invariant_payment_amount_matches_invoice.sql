-- Money integrity: every attempt charges exactly the invoiced amount, in the invoice currency.
select p.payment_key, p.amount_minor, i.amount_minor as invoice_amount_minor
from {{ ref('fct_payments') }} as p
inner join {{ ref('stg_invoices') }} as i using (invoice_id)
where {{ partition_bounds('p.event_date') }}
    and (p.amount_minor <> i.amount_minor or p.currency <> i.currency or p.amount_minor <= 0)
