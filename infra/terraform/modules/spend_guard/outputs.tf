output "topic" {
  value = google_pubsub_topic.budget.id
}

output "service_uri" {
  value = module.service.uri
}

output "kill_at_gbp" {
  value = var.kill_at_gbp
}
