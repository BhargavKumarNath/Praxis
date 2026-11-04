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

variable "publisher_members" {
  description = "Static name -> runtime identity allowed to publish to the events topic (s17)."
  type        = map(string)
  default     = {}
}

variable "subscriber_members" {
  description = "Subscription role (operational, warehouse, ...) -> the one identity consuming it."
  type        = map(string)
  default     = {}
}

variable "dead_letter_publisher_members" {
  description = "Static name -> identity whose dead-letter router publishes to the DLQ topic."
  type        = map(string)
  default     = {}
}
