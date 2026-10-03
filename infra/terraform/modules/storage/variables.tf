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
