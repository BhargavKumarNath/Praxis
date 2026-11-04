# Daily triggers for Cloud Run jobs (ADR 0027): the live tick and one ops-monitor tick.
#
# Cost: Cloud Scheduler bills per job per month beyond the free allowance (3 jobs per
# billing account); 2 jobs here, so at most about USD 0.20 / month even with no allowance
# left. Each trigger only calls the Cloud Run Admin API's jobs.run for ONE job, as the
# scheduler identity, which holds roles/run.invoker on exactly those jobs (cloud_run_job
# module) and nothing else. A failed trigger is not retried: the next day's tick catches up.
resource "google_cloud_scheduler_job" "job" {
  for_each         = var.jobs
  name             = "${var.prefix}-${var.environment}-${each.key}"
  project          = var.project_id
  region           = var.location
  description      = each.value.description
  schedule         = each.value.cron
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.location}/jobs/${each.value.job}:run"

    oauth_token {
      service_account_email = var.service_account
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}
