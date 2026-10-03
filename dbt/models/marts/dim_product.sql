with products as (
    select product from {{ ref('stg_usage') }}
    union distinct
    select product from {{ ref('stg_price_exposures') }}
),

ranked as (
    select
        product,
        list_price_micros,
        occurred_at,
        row_number() over (partition by product order by occurred_at desc, event_id desc) as rn,
        min(occurred_at) over (partition by product) as first_priced_at
    from {{ ref('stg_price_exposures') }}
)

select
    p.product as product_id,
    r.list_price_micros as latest_list_price_micros,
    r.first_priced_at
from products as p
left join ranked as r on p.product = r.product and r.rn = 1
