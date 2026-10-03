# Raw immutable lake bucket. Append-only by convention; versioning protects against overwrite.
resource "google_storage_bucket" "raw" {
  name                        = "${var.prefix}-${var.environment}-raw-${var.project_id}"
  project                     = var.project_id
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age        = var.noncurrent_version_retention_days
      with_state = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  labels = var.labels
}
