# One Cloud Run job (deployment, ADR 0026): bounded, on-demand work (migrations, event
# consumers that drain and exit, one ops-monitor tick). A job costs nothing while it is not
# executing; every execution has one task, no retries and a hard timeout, so a fault can never
# loop. Executions are owner actions (`gcloud run jobs execute ... --args=...`).
#
# google-beta: the GA provider 6.x has no `mount_options` for Cloud Storage volumes, and the
# live job needs uid / gid so its non-root user (10001) can write the mount (ADR 0027).
resource "google_cloud_run_v2_job" "job" {
  provider            = google-beta
  name                = "${var.prefix}-${var.environment}-${var.name}"
  project             = var.project_id
  location            = var.location
  deletion_protection = false
  labels              = var.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = var.service_account
      timeout         = "${var.timeout_s}s"
      max_retries     = 0
      # Gen2 is required for a Cloud Storage volume (ADR 0027).
      execution_environment = var.gcs_volume == null ? null : "EXECUTION_ENVIRONMENT_GEN2"

      dynamic "volumes" {
        for_each = var.gcs_volume == null ? [] : [var.gcs_volume]
        content {
          name = "live"
          gcs {
            bucket        = volumes.value.bucket
            read_only     = volumes.value.read_only
            mount_options = length(volumes.value.mount_options) > 0 ? volumes.value.mount_options : null
          }
        }
      }

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
        image   = var.image
        command = var.command
        args    = var.args

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        dynamic "volume_mounts" {
          for_each = var.gcs_volume == null ? [] : [var.gcs_volume]
          content {
            name       = "live"
            mount_path = volume_mounts.value.mount_path
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
    }
  }
}

# Who may start an execution (e.g. the scheduler's identity). Static keys: the members are
# service-account emails known only at apply time.
resource "google_cloud_run_v2_job_iam_member" "invoker" {
  for_each = var.invokers
  project  = var.project_id
  location = var.location
  name     = google_cloud_run_v2_job.job.name
  role     = "roles/run.invoker"
  member   = each.value
}
