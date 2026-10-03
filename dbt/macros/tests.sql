{# Generic test: the listed columns together identify at most one row. No dbt_utils needed. #}
{% test unique_grain(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n_rows
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
