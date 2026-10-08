# Event topic, DLQ topic, one pull subscription per consumer role, and a DLQ inspector.
# Delivery is at-least-once; consumers must be idempotent (ADR 0001, ADR 0009).
# Names, retry and DLQ policy mirror src/praxis/streaming/topology.py; a static test keeps
# them in sync (tests/streaming/test_topology.py).
data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Pub/Sub's service agent forwards dead letters; it needs publish on the DLQ topic and
  # subscribe on every source subscription, or forwarding silently never happens.
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

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

# Only control-plane (stateful) events reach Postgres; bulk usage never does (ADR 0002).
# Filters are immutable after creation and limited to 256 bytes.
resource "google_pubsub_subscription" "operational" {
  name                       = "${var.prefix}-${var.environment}-events-operational"
  project                    = var.project_id
  topic                      = google_pubsub_topic.events.id
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention_duration
  retain_acked_messages      = false
  filter                     = "attributes.stateful = \"true\""

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

resource "google_pubsub_subscription" "warehouse" {
  name                       = "${var.prefix}-${var.environment}-events-warehouse"
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

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

resource "google_pubsub_subscription" "monitoring" {
  name                       = "${var.prefix}-${var.environment}-events-monitoring"
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

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

# Phase 8: payment and churn events drive dunning decisions (ADR 0015). Stateful only.
resource "google_pubsub_subscription" "dunning" {
  name                       = "${var.prefix}-${var.environment}-events-dunning"
  project                    = var.project_id
  topic                      = google_pubsub_topic.events.id
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention_duration
  retain_acked_messages      = false
  filter                     = "attributes.stateful = \"true\""

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

resource "google_pubsub_subscription" "dead_letter_inspect" {
  name                       = "${var.prefix}-${var.environment}-events-dlq-inspect"
  project                    = var.project_id
  topic                      = google_pubsub_topic.dead_letter.id
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention_duration

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dlq_source_subscriber" {
  for_each = {
    operational = google_pubsub_subscription.operational.name
    warehouse   = google_pubsub_subscription.warehouse.name
    monitoring  = google_pubsub_subscription.monitoring.name
    dunning     = google_pubsub_subscription.dunning.name
  }
  project      = var.project_id
  subscription = each.value
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
