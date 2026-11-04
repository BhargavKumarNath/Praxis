# The Google APIs this environment uses, and nothing else (deployment, 2026-11-01).
# disable_on_destroy = false: a destroy removes the resources, never the APIs other tooling
# (gcloud, the BigQuery sandbox data) may still rely on.
resource "google_project_service" "api" {
  for_each                   = toset(var.services)
  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
