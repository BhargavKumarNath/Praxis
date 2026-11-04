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

variable "layers" {
  type    = list(string)
  default = ["raw", "staging", "marts"]
}

variable "default_partition_expiration_days" {
  type    = number
  default = 60
}

variable "default_table_expiration_days" {
  description = "Matches the Phase 2 datasets (60 days), so importing them changes nothing."
  type        = number
  default     = 60
}

variable "delete_contents_on_destroy" {
  type    = bool
  default = false
}

variable "labels" {
  type    = map(string)
  default = {}
}
