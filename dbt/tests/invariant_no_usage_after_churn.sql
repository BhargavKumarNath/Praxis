-- A churned customer must not be billed for usage on a later day.
select u.customer_id, u.event_date, c.churned_at
from {{ ref('fct_usage_daily') }} as u
inner join {{ ref('dim_customer') }} as c using (customer_id)
where u.event_date between date '{{ var("start_date") }}' and date '{{ var("end_date") }}'
    and c.is_churned
    and u.event_date > cast(c.churned_at as date)
