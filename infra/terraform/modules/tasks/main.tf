# Cloud Tasks queue for scheduled payment retries (Phase 8, ADR 0015). Declared and
# validated only; nothing is applied without explicit user approval (docs/cost-guard.md).
# Mirrors praxis.dunning.tasks: named tasks (deduplication), HTTP target with an OIDC token,
# at-least-once delivery handled by the idempotent RetryExecutor. Bounded dispatch rate and
# retries keep a misbehaving handler from generating unbounded work.
resource "google_cloud_tasks_queue" "payment_retries" {
  name     = "${var.prefix}-${var.environment}-payment-retries"
  project  = var.project_id
  location = var.location

  rate_limits {
    max_dispatches_per_second = var.max_dispatches_per_second
    max_concurrent_dispatches = var.max_concurrent_dispatches
  }

  retry_config {
    max_attempts       = var.max_attempts
    max_retry_duration = "86400s" # matches policy max_job_lateness_hours = 24
    min_backoff        = "10s"
    max_backoff        = "600s"
    max_doublings      = 5
  }

  stackdriver_logging_config {
    sampling_ratio = 1.0
  }
}

# Only the dunning service may create / delete tasks; least privilege (CLAUDE.md s17).
resource "google_cloud_tasks_queue_iam_member" "enqueuer" {
  count    = var.enqueuer_member == null ? 0 : 1
  project  = var.project_id
  location = var.location
  name     = google_cloud_tasks_queue.payment_retries.name
  role     = "roles/cloudtasks.enqueuer"
  member   = var.enqueuer_member
}
