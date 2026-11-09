# One scale-to-zero Cloud Run service (Phase 15, ADR 0024). Declared only.
#
# Cost (docs/cost-guard.md): min instances 0 (no idle charge), request-based billing
# (`cpu_idle = true`: CPU is billed only while a request is in flight), and a hard
# max-instance cap validated here AND in praxis.config (Settings.cloud_run_max_instances <= 10).
# Security: the approving console is never public; `public = true` is refused by validation
# for every service except the API (Stripe must reach the webhook) and the read-only
# console-public copy, which holds no approver secret (ADR 0032).
resource "google_cloud_run_v2_service" "service" {
  name                = "${var.prefix}-${var.environment}-${var.name}"
  project             = var.project_id
  location            = var.location
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection
  labels              = var.labels

  # Service-level floor too (the API reports it; leaving it undeclared drifts every plan):
  # no instance is ever kept warm, at either level.
  scaling {
    min_instance_count = 0
  }

  template {
    service_account                  = var.service_account
    timeout                          = "${var.request_timeout_s}s"
    max_instance_request_concurrency = var.concurrency
    # Gen2 is required for a Cloud Storage volume (ADR 0027); unset keeps the default.
    execution_environment = var.gcs_volume == null ? null : "EXECUTION_ENVIRONMENT_GEN2"

    # The live volume (Cloud Storage FUSE), shared by every container of the instance.
    dynamic "volumes" {
      for_each = var.gcs_volume == null ? [] : [var.gcs_volume]
      content {
        name = "live"
        gcs {
          bucket    = volumes.value.bucket
          read_only = volumes.value.read_only
        }
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    # Direct VPC egress (no connector, no charge of its own): private ranges only, so the
    # service reaches Postgres on the private subnet and Google APIs over Google's network.
    dynamic "vpc_access" {
      for_each = var.vpc_egress == null ? [] : [var.vpc_egress]
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = vpc_access.value.network
          subnetwork = vpc_access.value.subnetwork
        }
      }
    }

    containers {
      name       = var.name
      image      = var.image
      command    = var.command
      args       = var.args
      depends_on = [for s in var.sidecars : s.name]

      ports {
        container_port = 8080
      }

      dynamic "volume_mounts" {
        for_each = var.gcs_volume == null ? [] : [var.gcs_volume]
        content {
          name       = "live"
          mount_path = volume_mounts.value.mount_path
        }
      }

      resources {
        cpu_idle          = true
        startup_cpu_boost = false
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }

    # A sidecar shares the instance's network with the ingress container (e.g. the console
    # API behind the console UI). Cloud Run routes outside traffic only to the ingress
    # container's port, so a sidecar is never reachable from outside the instance.
    dynamic "containers" {
      for_each = var.sidecars
      content {
        name    = containers.value.name
        image   = containers.value.image
        command = containers.value.command
        args    = containers.value.args

        resources {
          cpu_idle = true
          limits = {
            cpu    = containers.value.cpu
            memory = containers.value.memory
          }
        }


        dynamic "volume_mounts" {
          for_each = var.gcs_volume == null ? [] : [var.gcs_volume]
          content {
            name       = "live"
            mount_path = volume_mounts.value.mount_path
          }
        }

        startup_probe {
          tcp_socket {
            port = containers.value.port
          }
          period_seconds    = 2
          failure_threshold = 30
        }

        dynamic "env" {
          for_each = merge(var.env, containers.value.env)
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = containers.value.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.public ? 1 : 0
  project  = var.project_id
  location = var.location
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = toset(var.invokers)
  project  = var.project_id
  location = var.location
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = each.value
}

# Identities known only at apply time (service accounts) need static keys for for_each.
resource "google_cloud_run_v2_service_iam_member" "service_invoker" {
  for_each = var.service_invokers
  project  = var.project_id
  location = var.location
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = each.value
}
