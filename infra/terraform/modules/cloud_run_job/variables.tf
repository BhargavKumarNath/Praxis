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

variable "name" {
  type = string
}

variable "image" {
  type = string
}

variable "service_account" {
  type = string
}

variable "command" {
  type    = list(string)
  default = null
}

variable "args" {
  type    = list(string)
  default = null
}

variable "timeout_s" {
  type    = number
  default = 600
  validation {
    condition     = var.timeout_s >= 10 && var.timeout_s <= 14400
    error_message = "A job execution is bounded: 10 s to 4 h."
  }
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "1Gi"
}

variable "env" {
  type    = map(string)
  default = {}
}

variable "secret_env" {
  type    = map(string)
  default = {}
}

variable "vpc_egress" {
  type = object({
    network    = string
    subnetwork = string
  })
  default = null
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "gcs_volume" {
  description = "A Cloud Storage bucket mounted in the job's container (ADR 0027)."
  type = object({
    bucket        = string
    mount_path    = string
    read_only     = bool
    mount_options = optional(list(string), [])
  })
  default = null
}

variable "invokers" {
  description = "Static key => member allowed to start executions (roles/run.invoker)."
  type        = map(string)
  default     = {}
}
