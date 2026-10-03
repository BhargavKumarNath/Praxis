-- Every simulated region must map to a real signal anchor, or features silently go null.
select distinct c.region_id
from {{ ref('dim_customer') }} as c
left join {{ ref('dim_region') }} as r using (region_id)
where r.region_id is null
