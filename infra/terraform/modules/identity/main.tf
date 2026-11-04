# Runtime identities and their secrets (Phase 15, ADR 0024). Declared only; nothing is applied
# without explicit owner approval (docs/cost-guard.md).
#
# Least privilege (CLAUDE.md s17): one service account per process, NO project-level roles at
# all. Every grant below is on a single resource (a secret here; a topic, subscription, queue
# or dataset in the module that owns it). Secret VALUES are never in Terraform: only the
# secret containers are declared, versions are added out of band (`gcloud secrets versions
# add`), so no secret ever reaches the Terraform state.
locals {
  services = {
    api       = "Praxis API (forecasts, Stripe webhooks, retry callbacks)"
    console   = "Praxis decision console (read model, approvals)"
    streaming = "Praxis event consumers"
    ops       = "Praxis ops monitor"
    agent     = "Praxis AI operator (no secrets except the LLM key)"
    db        = "Praxis control-plane Postgres VM (its password, its image)"
    guard     = "Praxis spend guard (unlinks billing at the spend ceiling)"
    live      = "Praxis live tick (synthetic world, warehouse, model lifecycle; ADR 0027)"
    scheduler = "Praxis scheduler (may only start the live and ops jobs)"
  }
  # Which process may read which secret. Nothing else can.
  secret_readers = {
    database-url          = ["api", "console", "streaming", "ops", "agent", "live"]
    stripe-webhook-secret = ["api"]
    stripe-api-key        = ["api"]
    task-token            = ["api"]
    console-approvers     = ["console"]
    llm-api-key           = ["agent"]
    postgres-password     = ["db"]
  }
  grants = merge([
    for secret, readers in local.secret_readers : {
      for r in readers : "${secret}/${r}" => { secret = secret, reader = r }
    }
  ]...)
}

resource "google_service_account" "service" {
  for_each     = local.services
  project      = var.project_id
  account_id   = "${var.prefix}-${var.environment}-${each.key}"
  display_name = each.value
}

resource "google_secret_manager_secret" "secret" {
  for_each  = local.secret_readers
  project   = var.project_id
  secret_id = "${var.prefix}-${var.environment}-${each.key}"
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.location
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "reader" {
  for_each  = local.grants
  project   = var.project_id
  secret_id = google_secret_manager_secret.secret[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.service[each.value.reader].email}"
}
