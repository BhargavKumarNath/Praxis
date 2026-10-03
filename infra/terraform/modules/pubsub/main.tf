# One topic per event stream, a pull subscription with bounded retries, and a DLQ topic.
# Delivery is at-least-once; consumers must be idempotent (see ADR 0001).
resource "google_pubsub_topic" "events" {
  name                       = "${var.prefix}-${var.environment}-events"
  project                    = var.project_id
  message_retention_duration = var.message_retention_duration
  labels                     = var.labels
}

resource "google_pubsub_topic" "dead_letter" {
  name                       = "${var.prefix}-${var.environment}-events-dlq"
  project                    = var.project_id
  message_retention_duration = var.message_retention_duration
  labels                     = var.labels
}

resource "google_pubsub_subscription" "operational" {
  name                       = "${var.prefix}-${var.environment}-events-operational"
  project                    = var.project_id
  topic                      = google_pubsub_topic.events.id
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention_duration
  retain_acked_messages      = false

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  labels = var.labels
}

resource "google_pubsub_subscription" "dead_letter_inspect" {
  name                       = "${var.prefix}-${var.environment}-events-dlq-inspect"
  project                    = var.project_id
  topic                      = google_pubsub_topic.dead_letter.id
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention_duration
  labels                     = var.labels
}
