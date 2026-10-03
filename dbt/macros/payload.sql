{# Typed extraction from the JSON event payload. DuckDB is the tested implementation;
   the BigQuery variants are written to the same contract but have never been executed. #}
{% macro payload_str(path) %}{{ return(adapter.dispatch('payload_str')(path)) }}{% endmacro %}
{% macro duckdb__payload_str(path) %}json_extract_string(payload, '$.{{ path }}'){% endmacro %}
{% macro bigquery__payload_str(path) %}json_value(payload, '$.{{ path }}'){% endmacro %}

{# Native type names per adapter (BigQuery has no VARCHAR or DOUBLE). #}
{% macro type_int() %}{{ return(adapter.dispatch('type_int')()) }}{% endmacro %}
{% macro duckdb__type_int() %}bigint{% endmacro %}
{% macro bigquery__type_int() %}int64{% endmacro %}
{% macro type_float() %}{{ return(adapter.dispatch('type_float')()) }}{% endmacro %}
{% macro duckdb__type_float() %}double{% endmacro %}
{% macro bigquery__type_float() %}float64{% endmacro %}
{% macro type_bool() %}{{ return(adapter.dispatch('type_bool')()) }}{% endmacro %}
{% macro duckdb__type_bool() %}boolean{% endmacro %}
{% macro bigquery__type_bool() %}bool{% endmacro %}
{% macro type_text() %}{{ return(adapter.dispatch('type_text')()) }}{% endmacro %}
{% macro duckdb__type_text() %}varchar{% endmacro %}
{% macro bigquery__type_text() %}string{% endmacro %}

{% macro payload_int(path) %}cast({{ payload_str(path) }} as {{ type_int() }}){% endmacro %}
{% macro payload_float(path) %}cast({{ payload_str(path) }} as {{ type_float() }}){% endmacro %}
{% macro payload_bool(path) %}cast({{ payload_str(path) }} as {{ type_bool() }}){% endmacro %}
{% macro payload_date(path) %}cast({{ payload_str(path) }} as date){% endmacro %}

{# Portable date arithmetic. #}
{% macro add_days(date_expr, n) %}{{ return(adapter.dispatch('add_days')(date_expr, n)) }}{% endmacro %}
{% macro duckdb__add_days(date_expr, n) %}({{ date_expr }} + cast({{ n }} as integer)){% endmacro %}
{% macro bigquery__add_days(date_expr, n) %}date_add({{ date_expr }}, interval {{ n }} day){% endmacro %}
