{#
  Region-day feature view. A row for feature_date D uses only information available at the end
  of day D:
    * service, usage, payment and weather/carbon/EIA values aggregate observations dated D;
    * macro values (FRED) are taken as-of D minus the release lag, because FRED publishes a
      period weeks after it ends. macro_*_observed_date records exactly which observation was
      used so the no-leakage test can verify it.
  Every fact read is bounded by the partition column (BigQuery requires it).
#}
{% set start_date = "date '" ~ var('start_date') ~ "'" %}
{% set end_date = "date '" ~ var('end_date') ~ "'" %}
{% set macro_start = add_days(start_date, -400) %}

with service as (
    select
        region_id,
        event_date as feature_date,
        avg(utilization) as avg_utilization,
        max(utilization) as max_utilization,
        avg(error_rate) as avg_error_rate,
        avg(latency_p95_ms) as avg_latency_p95_ms
    from {{ ref('fct_service_metrics_hourly') }}
    where event_date between {{ start_date }} and {{ end_date }}
    group by region_id, event_date
),

usage as (
    select
        region_id,
        event_date as feature_date,
        sum(units) as usage_units,
        sum(throttled_units) as throttled_units,
        count(distinct customer_id) as active_customers
    from {{ ref('fct_usage_daily') }}
    where event_date between {{ start_date }} and {{ end_date }}
    group by region_id, event_date
),

payments as (
    select
        c.region_id,
        p.event_date as feature_date,
        count(*) as payment_attempts,
        sum(case when p.outcome = 'failed' then 1 else 0 end) as payment_failures
    from {{ ref('fct_payments') }} as p
    inner join {{ ref('dim_customer') }} as c on p.customer_id = c.customer_id
    where p.event_date between {{ start_date }} and {{ end_date }}
    group by c.region_id, p.event_date
),

weather as (
    select
        r.region_id,
        s.observed_date as feature_date,
        avg(case when s.metric = 'temperature_2m' then s.value end) as temperature_c_mean,
        avg(case when s.metric = 'relative_humidity_2m' then s.value end) as humidity_pct_mean,
        avg(case when s.metric = 'wind_speed_10m' then s.value end) as wind_kmh_mean,
        avg(case when s.metric = 'cloud_cover' then s.value end) as cloud_cover_pct_mean,
        sum(case when s.metric = 'precipitation' then s.value end) as precipitation_mm_total
    from {{ ref('fct_external_signals') }} as s
    inner join {{ ref('dim_region') }} as r on s.entity_id = r.region_id
    where s.source = 'open_meteo'
        and s.observed_date between {{ start_date }} and {{ end_date }}
    group by r.region_id, s.observed_date
),

carbon as (
    select
        r.region_id,
        s.observed_date as feature_date,
        avg(s.value) as carbon_intensity_gco2_kwh_mean
    from {{ ref('fct_external_signals') }} as s
    inner join {{ ref('dim_region') }} as r on s.entity_id = r.carbon_area
    where s.source = 'carbon_intensity'
        and s.metric = 'carbon_intensity_actual'
        and s.observed_date between {{ start_date }} and {{ end_date }}
    group by r.region_id, s.observed_date
),

grid_demand as (
    select
        r.region_id,
        s.observed_date as feature_date,
        avg(s.value) as grid_demand_mwh_mean
    from {{ ref('fct_external_signals') }} as s
    inner join {{ ref('dim_region') }} as r on s.entity_id = r.eia_respondent
    where s.source = 'eia'
        and s.observed_date between {{ start_date }} and {{ end_date }}
    group by r.region_id, s.observed_date
),

macro_candidates as (
    select
        d.feature_date,
        s.metric,
        s.value,
        s.observed_date,
        row_number() over (
            partition by d.feature_date, s.metric order by s.observed_date desc
        ) as rn
    from (select distinct feature_date from service) as d
    inner join {{ ref('fct_external_signals') }} as s
        on {{ add_days('s.observed_date', var('macro_release_lag_days')) }} <= d.feature_date
    where s.source = 'fred'
        and s.observed_date between {{ macro_start }} and {{ end_date }}
),

macro as (
    select
        feature_date,
        max(case when metric = 'cpiaucsl' then value end) as macro_cpiaucsl,
        max(case when metric = 'cpiaucsl' then observed_date end) as macro_cpiaucsl_observed_date,
        max(case when metric = 'fedfunds' then value end) as macro_fedfunds,
        max(case when metric = 'fedfunds' then observed_date end) as macro_fedfunds_observed_date,
        max(case when metric = 'ppiaco' then value end) as macro_ppiaco,
        max(case when metric = 'ppiaco' then observed_date end) as macro_ppiaco_observed_date
    from macro_candidates
    where rn = 1
    group by feature_date
)

select
    s.region_id,
    s.feature_date,
    s.avg_utilization,
    s.max_utilization,
    s.avg_error_rate,
    s.avg_latency_p95_ms,
    u.usage_units,
    u.throttled_units,
    u.active_customers,
    p.payment_attempts,
    p.payment_failures,
    w.temperature_c_mean,
    w.humidity_pct_mean,
    w.wind_kmh_mean,
    w.cloud_cover_pct_mean,
    w.precipitation_mm_total,
    c.carbon_intensity_gco2_kwh_mean,
    g.grid_demand_mwh_mean,
    m.macro_cpiaucsl,
    m.macro_cpiaucsl_observed_date,
    m.macro_fedfunds,
    m.macro_fedfunds_observed_date,
    m.macro_ppiaco,
    m.macro_ppiaco_observed_date
from service as s
left join usage as u on s.region_id = u.region_id and s.feature_date = u.feature_date
left join payments as p on s.region_id = p.region_id and s.feature_date = p.feature_date
left join weather as w on s.region_id = w.region_id and s.feature_date = w.feature_date
left join carbon as c on s.region_id = c.region_id and s.feature_date = c.feature_date
left join grid_demand as g on s.region_id = g.region_id and s.feature_date = g.feature_date
left join macro as m on s.feature_date = m.feature_date
