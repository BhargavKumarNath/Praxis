variable "project_id" {
  type = string
}

variable "location" {
  type = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "environment" {
  type = string
}

variable "max_dispatches_per_second" {
  type    = number
  default = 5
}

variable "max_concurrent_dispatches" {
  type    = number
  default = 10
}

variable "max_attempts" {
  type    = number
  default = 20
}

variable "enqueuer_member" {
  description = "IAM member (e.g. serviceAccount:...) allowed to create and delete tasks."
  type        = string
  default     = null
}
