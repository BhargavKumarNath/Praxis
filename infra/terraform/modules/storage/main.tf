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

# Live-loop volume (ADR 0027): the SYNTHETIC warehouse, model artifacts, tick reports and
# state. The live job mounts it read-write; the API, console and ops monitor read-only.
# Versioned so that a reader holding the previous warehouse generation keeps a consistent
# file while the tick replaces it; noncurrent generations are deleted after 2 days.
# Size ~0.2-1 GB (Standard, us-central1: inside the 5 GB Free Tier).
resource "google_storage_bucket" "live" {
  name                        = "${var.prefix}-${var.environment}-live-${var.project_id}"
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
      days_since_noncurrent_time = 2
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  labels = var.labels
}

resource "google_storage_bucket_iam_member" "live_writer" {
  for_each = var.live_writers
  bucket   = google_storage_bucket.live.name
  role     = "roles/storage.objectUser"
  member   = each.value
}

resource "google_storage_bucket_iam_member" "live_reader" {
  for_each = var.live_readers
  bucket   = google_storage_bucket.live.name
  role     = "roles/storage.objectViewer"
  member   = each.value
}
