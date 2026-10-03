# Analytical datasets. Tables arrive in Phase 2 with partitioning; the default partition
# expiration here is a cost guard for development data.
resource "google_bigquery_dataset" "layer" {
  for_each = toset(var.layers)

  dataset_id                      = "${var.prefix}_${var.environment}_${each.value}"
  project                         = var.project_id
  location                        = var.location
  default_partition_expiration_ms = var.default_partition_expiration_days * 24 * 60 * 60 * 1000
  delete_contents_on_destroy      = var.delete_contents_on_destroy
  labels                          = var.labels
}
