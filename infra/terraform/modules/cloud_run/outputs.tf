output "name" {
  value = google_cloud_run_v2_service.service.name
}

output "uri" {
  value = google_cloud_run_v2_service.service.uri
}

output "max_instances" {
  value = var.max_instances
}

output "public" {
  value = var.public
}
