variable "project_id" {
  type = string
}

variable "environment" {
  type = string
}

variable "location" {
  type = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "name" {
  type = string
}

variable "image" {
  description = "Container image (pinned by digest when deployed)."
  type        = string
}

variable "command" {
  type    = list(string)
  default = null
}

variable "args" {
  type    = list(string)
  default = null
}

variable "vpc_egress" {
  description = "Direct VPC egress {network, subnetwork}; null = no VPC access."
  type = object({
    network    = string
    subnetwork = string
  })
  default = null
}

variable "sidecars" {
  description = "Loopback-only containers next to the ingress container (all env vars shared)."
  type = list(object({
    name       = string
    image      = string
    port       = number
    command    = optional(list(string))
    args       = optional(list(string))
    cpu        = optional(string, "1")
    memory     = optional(string, "512Mi")
    env        = optional(map(string), {})
    secret_env = optional(map(string), {})
  }))
  default = []
}

variable "service_account" {
  type = string
}

variable "max_instances" {
  description = "Hard cap on instances; mirrors Settings.cloud_run_max_instances (<= 10)."
  type        = number
  default     = 2
  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 10
    error_message = "max_instances must be between 1 and 10 (docs/cost-guard.md rule 3)."
  }
}

variable "concurrency" {
  type    = number
  default = 40
}

variable "request_timeout_s" {
  description = "Bounded request time; long work belongs on Pub/Sub or Cloud Tasks."
  type        = number
  default     = 30
  validation {
    condition     = var.request_timeout_s >= 1 && var.request_timeout_s <= 60
    error_message = "request_timeout_s must be 1..60 seconds."
  }
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

variable "env" {
  type    = map(string)
  default = {}
}

variable "secret_env" {
  description = "Env var name -> Secret Manager secret id (value read at start, never in state)."
  type        = map(string)
  default     = {}
}

variable "public" {
  description = "Grant run.invoker to allUsers. Only the API may (Stripe webhooks)."
  type        = bool
  default     = false
  validation {
    condition     = !(var.public && var.name == "console")
    error_message = "The decision console is never public (ADR 0021, ADR 0024)."
  }
}

variable "invokers" {
  description = "Named principals allowed to invoke a private service."
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.invokers, "allUsers") && !contains(var.invokers, "allAuthenticatedUsers")
    error_message = "Use `public` (API only), never allUsers / allAuthenticatedUsers in invokers."
  }
}

variable "service_invokers" {
  description = "Static name -> service-account member allowed to invoke (e.g. a push subscription)."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for m in values(var.service_invokers) : startswith(m, "serviceAccount:")])
    error_message = "service_invokers holds service accounts only."
  }
}

variable "deletion_protection" {
  description = "Dev must be destroyable (Phase 15 gate); prod sets true."
  type        = bool
  default     = false
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "gcs_volume" {
  description = "A Cloud Storage bucket mounted in every container (the live volume, ADR 0027)."
  type = object({
    bucket     = string
    mount_path = string
    read_only  = bool
  })
  default = null
}
