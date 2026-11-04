variable "project_id" {
  type = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "environment" {
  type = string
}

variable "location" {
  type = string
}

variable "image" {
  type = string
}

variable "service_account" {
  description = "The guard's own identity: push OIDC token, run.invoker, billing.projectManager."
  type        = string
}

variable "kill_at_gbp" {
  type = number
  validation {
    condition     = var.kill_at_gbp > 0 && var.kill_at_gbp <= 10
    error_message = "The guard must act at or below the GBP 10 project ceiling."
  }
}

variable "budget_display_name" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}
