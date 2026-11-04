variable "project_id" {
  type = string
}

variable "environment" {
  type = string
}

variable "location" {
  description = "Secret replica location (one region, next to the services that read it)."
  type        = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "labels" {
  type    = map(string)
  default = {}
}
