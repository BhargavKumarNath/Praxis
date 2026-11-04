# One Docker repository for every Praxis image (deployment, ADR 0026). Images are deployed by
# digest. Retention: each package keeps its 3 most recent versions; anything older than 7 days
# beyond those is deleted (Artifact Registry's free 0.5 GB is per billing account and shared).
resource "google_artifact_registry_repository" "images" {
  repository_id          = var.prefix
  project                = var.project_id
  location               = var.location
  format                 = "DOCKER"
  description            = "Praxis container images (deployed by digest)"
  cleanup_policy_dry_run = false
  labels                 = var.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.keep_versions
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "604800s"
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "reader" {
  for_each   = var.reader_members
  project    = var.project_id
  location   = var.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}
