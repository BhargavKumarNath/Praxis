# Spend guards (Phase 15, ADR 0024). Declared only.
#
# 1. A billing budget in GBP (deployment: GBP 10, the owner's project ceiling) with alerts at
#    GBP 2 / 5 / 8 / 10 actual and 100 % of forecast, measured on GROSS cost (credits
#    excluded, so a promotional credit can never hide spend). A budget is a NOTIFICATION,
#    never a cap (docs/cost-guard.md); `notification_topic` feeds the spend guard
#    (modules/spend_guard), which is the automatic shutdown.
# 2. A BigQuery daily query quota on the project. It IS enforced (queries fail with
#    quotaExceeded) but Google documents it as approximate, so a query can overshoot.
#    Units: MiB per day for metric bigquery.googleapis.com/quota/query/usage, limit
#    /d/project. Verified 2026-11-01 (docs/cost-guard.md): the read-only Service Usage
#    listing reports unit "MiBy" (Mi = 2^20, By = byte) and limit unit "1/d/{project}" with
#    default 209715200 = the documented 200 TiB/day default. The resource is google-beta only.
data "google_project" "this" {
  project_id = var.project_id
}

resource "google_billing_budget" "monthly" {
  count           = var.billing_account == null ? 0 : 1
  billing_account = var.billing_account
  display_name    = "${var.prefix}-${var.environment}-monthly"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "GBP"
      units         = tostring(var.monthly_budget_gbp)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.alert_fractions
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  # Emails still go to the billing account's admins (default recipients); the topic, when
  # set, receives the budget state several times a day for the spend guard.
  dynamic "all_updates_rule" {
    for_each = var.notification_topic == null ? [] : [var.notification_topic]
    content {
      pubsub_topic                   = all_updates_rule.value
      schema_version                 = "1.0"
      disable_default_iam_recipients = false
    }
  }
}

resource "google_service_usage_consumer_quota_override" "bigquery_daily_query" {
  provider       = google-beta
  project        = var.project_id
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/query/usage")
  limit          = urlencode("/d/project")
  override_value = tostring(var.bigquery_daily_query_mib)
  force          = true
}
