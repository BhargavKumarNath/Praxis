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

variable "region" {
  type = string
}

variable "cidr" {
  description = "Shared by the database VM and Cloud Run Direct VPC egress (needs /26 or larger)."
  type        = string
  default     = "10.10.0.0/24"
}

variable "db_tag" {
  type    = string
  default = "praxis-db"
}
