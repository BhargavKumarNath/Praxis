output "events_topic" {
  value = google_pubsub_topic.events.name
}

output "dead_letter_topic" {
  value = google_pubsub_topic.dead_letter.name
}

output "subscriptions" {
  value = {
    operational = google_pubsub_subscription.operational.name
    warehouse   = google_pubsub_subscription.warehouse.name
    monitoring  = google_pubsub_subscription.monitoring.name
    dunning     = google_pubsub_subscription.dunning.name
    dlq_inspect = google_pubsub_subscription.dead_letter_inspect.name
  }
}
