select region_id, name as location_name, latitude, longitude, carbon_area, eia_respondent
from {{ source('raw', 'region_locations') }}
