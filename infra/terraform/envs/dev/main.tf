# Phase 0: declaration only. `terraform validate` runs; nothing is applied without
# explicit user approval (see docs/cost-guard.md).
provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  environment = "dev"
  labels = {
    app         = "praxis"
    environment = local.environment
    managed_by  = "terraform"
  }
}

module "storage" {
  source        = "../../modules/storage"
  project_id    = var.project_id
  environment   = local.environment
  location      = var.region
  force_destroy = true
  labels        = local.labels
}

module "pubsub" {
  source      = "../../modules/pubsub"
  project_id  = var.project_id
  environment = local.environment
  labels      = local.labels
}

module "bigquery" {
  source                     = "../../modules/bigquery"
  project_id                 = var.project_id
  environment                = local.environment
  location                   = var.region
  delete_contents_on_destroy = true
  labels                     = local.labels
}
