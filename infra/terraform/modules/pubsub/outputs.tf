output "events_topic" {
  value = google_pubsub_topic.events.name
}

output "dead_letter_topic" {
  value = google_pubsub_topic.dead_letter.name
}

output "operational_subscription" {
  value = google_pubsub_subscription.operational.name
}
