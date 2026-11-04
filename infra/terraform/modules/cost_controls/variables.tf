variable "project_id" {
  type = string
}

variable "environment" {
  type = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "billing_account" {
  description = "Billing account id; null (default) declares no budget."
  type        = string
  default     = null
}

variable "monthly_budget_gbp" {
  description = "Monthly budget in GBP: the owner's project ceiling (approved 2026-11-01: GBP 10 max, GBP 0-5 target)."
  type        = number
  default     = 10
  validation {
    condition     = var.monthly_budget_gbp > 0 && var.monthly_budget_gbp <= 10
    error_message = "The owner's ceiling is GBP 10 per month for the whole project; raising it needs owner approval."
  }
}

variable "alert_fractions" {
  description = "Actual-spend alert points as fractions of the budget (GBP 2 / 5 / 8 / 10 at GBP 10)."
  type        = list(number)
  default     = [0.2, 0.5, 0.8, 1.0]
}

variable "notification_topic" {
  description = "Pub/Sub topic id for programmatic budget notifications (the spend guard); null = email only."
  type        = string
  default     = null
}

variable "bigquery_daily_query_mib" {
  description = "Daily on-demand query cap in MiB. 10240 (10 GiB/day) <= 310 GiB/month, inside the 1 TiB monthly free tier."
  type        = number
  default     = 10240
  validation {
    condition     = var.bigquery_daily_query_mib > 0 && var.bigquery_daily_query_mib * 31 <= 1048576
    error_message = "31 days at this cap must stay inside the 1 TiB (1048576 MiB) monthly free tier."
  }
}
