variable "project_id" {
  type = string
}

variable "prefix" {
  type    = string
  default = "praxis"
}

variable "location" {
  type = string
}

variable "keep_versions" {
  type    = number
  default = 3
}

variable "reader_members" {
  description = "Static name -> identity that pulls outside Cloud Run (the database VM)."
  type        = map(string)
  default     = {}
}

variable "labels" {
  type    = map(string)
  default = {}
}
