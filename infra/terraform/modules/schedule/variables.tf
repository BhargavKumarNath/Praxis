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

variable "service_account" {
  description = "The scheduler identity (run.invoker on the target jobs only)."
  type        = string
}

variable "jobs" {
  description = "Trigger name => the Cloud Run job it starts and when (cron, UTC)."
  type = map(object({
    job         = string
    cron        = string
    description = string
  }))
  validation {
    condition     = length(var.jobs) <= 3
    error_message = "At most 3 triggers (the Cloud Scheduler free allowance per billing account)."
  }
  validation {
    condition     = alltrue([for j in values(var.jobs) : can(regex("^[0-9]{1,2} [0-9]{1,2} \\* \\* \\*$", j.cron))])
    error_message = "Each trigger runs at most once a day (a fixed minute and hour, every day)."
  }
}
