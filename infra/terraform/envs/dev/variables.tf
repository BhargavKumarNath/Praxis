variable "project_id" {
  description = "GCP project id. Supplied via TF_VAR_project_id or a gitignored tfvars file."
  type        = string
}

variable "region" {
  description = "Compute region: Free Tier e2-micro + Cloud Storage free tier (US regions only)."
  type        = string
  default     = "us-central1"
}

variable "bigquery_location" {
  description = "The Phase 2 datasets already live here; moving a dataset would destroy it."
  type        = string
  default     = "europe-west2"
}

variable "deploy_services" {
  description = "Declare the runtime (database VM, services, jobs, spend guard). Off by default: no image, GBP 0."
  type        = bool
  default     = false
}

variable "app_image" {
  description = "The Python image (API, console API, jobs, spend guard), by digest."
  type        = string
  default     = "us-central1-docker.pkg.dev/praxis/praxis/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

variable "web_image" {
  description = "The console UI image (Next standalone), by digest."
  type        = string
  default     = "us-central1-docker.pkg.dev/praxis/praxis/console-web@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

variable "postgres_image" {
  description = "The official postgres image mirrored into this project's registry, by digest."
  type        = string
  default     = "us-central1-docker.pkg.dev/praxis/praxis/postgres@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

variable "api_public" {
  description = "Grant allUsers on the API. Only for the Stripe webhook integration (owner decision)."
  type        = bool
  default     = false
}

variable "console_public" {
  description = "Deploy console-public: a read-only copy of the console that anyone may view (ADR 0032). It holds no approver secret, so every admin route answers 503."
  type        = bool
  default     = false
}

variable "stripe_enabled" {
  description = "Wire the Stripe webhook / retry-task secrets into the API (their versions must exist)."
  type        = bool
  default     = false
}

variable "console_invokers" {
  description = "Named principals (user:... / group:...) allowed to invoke the private services."
  type        = list(string)
  default     = []
}

variable "billing_account" {
  description = "Billing account for the GBP budget; null declares no budget (and no spend guard)."
  type        = string
  default     = null
}

variable "monthly_budget_gbp" {
  type    = number
  default = 10
}

variable "spend_guard_gbp" {
  description = "Actual gross spend (GBP) at which the spend guard unlinks billing."
  type        = number
  default     = 8
}
