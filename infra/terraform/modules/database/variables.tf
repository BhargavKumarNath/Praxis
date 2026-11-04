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
  description = "Must be a Free Tier e2-micro region."
  type        = string
  validation {
    condition     = contains(["us-central1", "us-west1", "us-east1"], var.region)
    error_message = "The Free Tier e2-micro exists only in us-central1, us-west1 and us-east1."
  }
}

variable "zone" {
  type = string
}

variable "subnetwork" {
  type = string
}

variable "allowed_cidr" {
  description = "The only source allowed to reach Postgres (the private subnet)."
  type        = string
}

variable "internal_ip" {
  description = "Fixed private address inside allowed_cidr; the database-url secret refers to it."
  type        = string
}

variable "network_tag" {
  type = string
}

variable "service_account" {
  type = string
}

variable "password_secret_id" {
  type = string
}

variable "postgres_image" {
  description = "Mirrored official postgres image in this project's Artifact Registry, by digest."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/.+@sha256:[0-9a-f]{64}$", var.postgres_image))
    error_message = "postgres_image must be an Artifact Registry image pinned by digest."
  }
}

variable "db_user" {
  type    = string
  default = "praxis"
}

variable "db_name" {
  type    = string
  default = "praxis"
}

variable "data_disk_gb" {
  description = "Boot 10 GB + data <= 30 GB-months keeps the disks inside the Free Tier."
  type        = number
  default     = 10
  validation {
    condition     = var.data_disk_gb >= 10 && var.data_disk_gb <= 20
    error_message = "10 GB boot + 10..20 GB data stays inside the 30 GB-month Free Tier."
  }
}

variable "snapshot_retention_days" {
  type    = number
  default = 7
}

variable "deletion_protection" {
  description = "Dev must be destroyable (Phase 15 gate)."
  type        = bool
  default     = false
}

variable "labels" {
  type    = map(string)
  default = {}
}
