variable "project_id" {
  description = "GCP project id. Supplied via TF_VAR_project_id or a gitignored tfvars file."
  type        = string
}

variable "region" {
  type    = string
  default = "europe-west2"
}
