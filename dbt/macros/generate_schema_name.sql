{# Use the configured schema verbatim (staging / marts), optionally prefixed.
   Locally the prefix is empty. For BigQuery set PRAXIS_BQ_SCHEMA_PREFIX=praxis_dev_ so the
   datasets match the Terraform names (<prefix>_<env>_<layer>). #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ var('schema_prefix') }}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
