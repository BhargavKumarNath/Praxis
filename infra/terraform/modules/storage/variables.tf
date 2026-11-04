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

variable "force_destroy" {
  description = "Allow terraform destroy to delete a non-empty bucket (disposable dev only)."
  type        = bool
  default     = false
}

variable "noncurrent_version_retention_days" {
  type    = number
  default = 30
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "live_writers" {
  description = "Static key => member that may write the live volume (the live job only)."
  type        = map(string)
  default     = {}
}

variable "live_readers" {
  description = "Static key => member that may read the live volume."
  type        = map(string)
  default     = {}
}
