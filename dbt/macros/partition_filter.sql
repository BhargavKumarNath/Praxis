{# BigQuery facts set require_partition_filter, so ANY query on them (including dbt tests) must
   bound the partition column. Tests are meant to see every row, so they use an explicit
   all-time constant range: it satisfies the requirement without hiding data. Cost note: a test
   then scans all partitions of that fact; acceptable at development volumes. #}
{% macro partition_bounds(column) -%}
    {{ column }} between date '1900-01-01' and date '2999-12-31'
{%- endmacro %}

{% macro partition_field(identifier) -%}
    {%- if identifier == 'fct_external_signals' -%}observed_date
    {%- elif identifier.startswith('fct_') -%}event_date
    {%- endif -%}
{%- endmacro %}

{# Overrides dbt's built-in so generic tests (unique, not_null, ...) on facts carry the bound. #}
{% macro get_where_subquery(relation) -%}
    {%- set field = partition_field(relation.identifier) | trim -%}
    {%- if target.type == 'bigquery' and field -%}
        {%- set base = "select * from " ~ relation ~ " where " ~ partition_bounds(field) | trim -%}
        {%- set where = config.get('where', '') -%}
        {%- if where -%}{%- set base = base ~ " and (" ~ where ~ ")" -%}{%- endif -%}
        {% do return("(" ~ base ~ ") dbt_subquery") %}
    {%- else -%}
        {% do return(dbt.default__get_where_subquery(relation)) %}
    {%- endif -%}
{%- endmacro %}
