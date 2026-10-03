-- Contract (UsageObserved): units >= 1 served; throttled_units >= 0 is the rejected remainder
-- (requested - served), so it is NOT bounded by units.
select event_date, customer_id, product, units, throttled_units
from {{ ref('fct_usage_daily') }}
where {{ partition_bounds('event_date') }} and (units < 1 or throttled_units < 0)
