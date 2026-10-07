{#
  One row per (hourly service metric, product): the marginal cost per billing unit that the
  infrastructure reports for each product in each region-hour (SYNTHETIC). The payload holds a
  product -> cost map; BigQuery needs literal JSON paths, so each product in the
  `marginal_cost_products` project var becomes one literal path (validated as a lower-snake-case
  identifier before it is placed in SQL). No query runs at compile time.
#}
{% set products = var('marginal_cost_products') %}
{% for p in products %}
    {% if not modules.re.fullmatch('[a-z][a-z0-9_]{0,63}', p) %}
        {{ exceptions.raise_compiler_error("unsafe product id: " ~ p) }}
    {% endif %}
{% endfor %}

{% for p in products %}
select
    event_id,
    entity_id as region_id,
    occurred_at,
    event_date,
    '{{ p }}' as product,
    {{ payload_int('marginal_cost_micros.' ~ p) }} as marginal_cost_micros
from {{ ref('stg_sim_events') }}
where event_type = 'service.metric_observed'
{% if not loop.last %}union all{% endif %}
{% endfor %}
