with latest_change as (
    select
        customer_id,
        to_tier,
        row_number() over (partition by customer_id order by changed_at desc, event_id desc) as rn
    from {{ ref('stg_subscription_changes') }}
)

select
    c.customer_id,
    c.region_id,
    c.initial_tier,
    coalesce(lc.to_tier, c.initial_tier) as current_tier,
    c.industry,
    c.preferred_payment_method,
    c.is_existing,
    c.created_at,
    ch.churned_at,
    ch.churn_reason,
    ch.churned_at is not null as is_churned
from {{ ref('stg_customers') }} as c
left join latest_change as lc on c.customer_id = lc.customer_id and lc.rn = 1
left join {{ ref('stg_churn') }} as ch on c.customer_id = ch.customer_id
