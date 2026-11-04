output "budget_gbp" {
  value = var.billing_account == null ? null : var.monthly_budget_gbp
}

output "bigquery_daily_query_mib" {
  value = var.bigquery_daily_query_mib
}

output "budget_display_name" {
  value = "${var.prefix}-${var.environment}-monthly"
}

output "alert_points_gbp" {
  value = var.billing_account == null ? null : [for f in var.alert_fractions : f * var.monthly_budget_gbp]
}
