-- Contract (ServiceMetricObserved): utilization = load / capacity, >= 0 and deliberately
-- unbounded above (shocks and spikes overload a region); error_rate is a probability.
select metric_id, utilization, error_rate, latency_p50_ms, latency_p95_ms
from {{ ref('fct_service_metrics_hourly') }}
where event_date between date '{{ var("start_date") }}' and date '{{ var("end_date") }}'
    and (utilization < 0 or error_rate < 0 or error_rate > 1 or latency_p95_ms < latency_p50_ms)
