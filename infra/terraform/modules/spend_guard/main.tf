# Automatic spend shutdown (deployment, ADR 0026). Google budgets only notify; this turns the
# notification into an action. The budget publishes its state to a topic several times a
# day; a push subscription delivers it (OIDC-authenticated) to a private Cloud Run service
# (praxis.api.spend_guard), which UNLINKS BILLING from the project once the budget's actual
# gross cost reaches kill_at_gbp. That stops every billable resource in the project at once.
# Google's documented pattern; Google warns resources may then be deleted, which is accepted
# for this disposable, synthetic-data environment. Billing data lags by hours, so this is a
# backstop under the bounded design, not a precise cap.
resource "google_pubsub_topic" "budget" {
  name                       = "${var.prefix}-${var.environment}-budget-alerts"
  project                    = var.project_id
  message_retention_duration = "86400s"
  labels                     = var.labels
}

# The Cloud Billing budget service publishes notifications as this Google-owned account.
resource "google_pubsub_topic_iam_member" "budget_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.budget.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:billing-budget-alert@system.gserviceaccount.com"
}

module "service" {
  source           = "../cloud_run"
  project_id       = var.project_id
  environment      = var.environment
  location         = var.location
  name             = "spend-guard"
  image            = var.image
  service_account  = var.service_account
  max_instances    = 1
  concurrency      = 1
  memory           = "512Mi"
  service_invokers = { push = "serviceAccount:${var.service_account}" }
  command          = ["uvicorn"]
  args             = ["--factory", "praxis.api.spend_guard:create_guard_app", "--host", "0.0.0.0", "--port", "8080"]
  env = {
    PRAXIS_ENVIRONMENT             = var.environment
    PRAXIS_GCP_PROJECT_ID          = var.project_id
    PRAXIS_SERVICE_NAME            = "praxis-spend-guard"
    PRAXIS_SPEND_GUARD_KILL_AT_GBP = tostring(var.kill_at_gbp)
    PRAXIS_SPEND_GUARD_BUDGET_NAME = var.budget_display_name
  }
  labels = var.labels
}

resource "google_pubsub_subscription" "push" {
  name                       = "${var.prefix}-${var.environment}-budget-alerts-guard"
  project                    = var.project_id
  topic                      = google_pubsub_topic.budget.id
  ack_deadline_seconds       = 30
  message_retention_duration = "86400s"

  push_config {
    push_endpoint = "${module.service.uri}/v1/budget"
    oidc_token {
      service_account_email = var.service_account
      audience              = module.service.uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

# The ONE project-level grant in Praxis (ADR 0026, pinned by tests/security): unlinking billing
# is a project operation, and roles/billing.projectManager is the narrowest role for it.
resource "google_project_iam_member" "unlink_billing" {
  project = var.project_id
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${var.service_account}"
}
