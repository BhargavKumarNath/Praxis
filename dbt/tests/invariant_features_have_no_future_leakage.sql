-- A macro value may only feed a row once its release lag has elapsed.
select region_id, feature_date, 'cpiaucsl' as series, macro_cpiaucsl_observed_date as observed_date
from {{ ref('feat_region_daily') }}
where macro_cpiaucsl_observed_date is not null
    and {{ add_days('macro_cpiaucsl_observed_date', var('macro_release_lag_days')) }} > feature_date
union all
select region_id, feature_date, 'fedfunds', macro_fedfunds_observed_date
from {{ ref('feat_region_daily') }}
where macro_fedfunds_observed_date is not null
    and {{ add_days('macro_fedfunds_observed_date', var('macro_release_lag_days')) }} > feature_date
union all
select region_id, feature_date, 'ppiaco', macro_ppiaco_observed_date
from {{ ref('feat_region_daily') }}
where macro_ppiaco_observed_date is not null
    and {{ add_days('macro_ppiaco_observed_date', var('macro_release_lag_days')) }} > feature_date
