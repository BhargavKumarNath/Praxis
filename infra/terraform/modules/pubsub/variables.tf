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

variable "message_retention_duration" {
  description = "Kept short to bound storage cost; the raw archive in GCS is the replay source."
  type        = string
  default     = "86400s"
}

variable "max_delivery_attempts" {
  type    = number
  default = 5

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100
    error_message = "Pub/Sub requires max_delivery_attempts between 5 and 100."
  }
}

variable "labels" {
  type    = map(string)
  default = {}
}
