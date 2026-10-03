select
    region_id,
    location_name,
    latitude,
    longitude,
    carbon_area,
    eia_respondent
from {{ ref('stg_region_locations') }}
