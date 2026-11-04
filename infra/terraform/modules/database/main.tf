# The control-plane Postgres (deployment, ADR 0026): one Free Tier e2-micro VM.
#
# Why a VM: the cheapest managed Postgres (Cloud SQL shared-core) costs more than the whole
# GBP 10 budget allows with headroom; Google's Free Tier covers one e2-micro and 30 GB-months
# of standard disk per billing account in us-central1 / us-west1 / us-east1.
#
# Shape: Container-Optimized OS (no package manager, auto-updating, read-only root), Shielded
# VM, OS Login, NO external IP. The official postgres image is mirrored into this project's
# Artifact Registry (pinned by digest) and pulled through Private Google Access. Data lives on
# a separate standard disk (survives VM replacement) with daily snapshots. The password is
# read at boot from Secret Manager into tmpfs; it never enters Terraform state, metadata or
# the image. Postgres listens only to the private subnet (VPC firewall + host iptables).
resource "google_compute_disk" "data" {
  name    = "${var.prefix}-${var.environment}-pgdata"
  project = var.project_id
  zone    = var.zone
  type    = "pd-standard"
  size    = var.data_disk_gb
  labels  = var.labels
}

resource "google_compute_resource_policy" "daily_snapshot" {
  name    = "${var.prefix}-${var.environment}-pgdata-daily"
  project = var.project_id
  region  = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "03:00"
      }
    }
    retention_policy {
      max_retention_days    = var.snapshot_retention_days
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      storage_locations = [var.region]
      labels            = var.labels
    }
  }
}

resource "google_compute_disk_resource_policy_attachment" "data" {
  name    = google_compute_resource_policy.daily_snapshot.name
  project = var.project_id
  disk    = google_compute_disk.data.name
  zone    = var.zone
}

resource "google_compute_address" "internal" {
  name         = "${var.prefix}-${var.environment}-db"
  project      = var.project_id
  region       = var.region
  subnetwork   = var.subnetwork
  address      = var.internal_ip # fixed: the database-url secret names it
  address_type = "INTERNAL"      # internal addresses are free; never an external one
}

resource "google_compute_instance" "db" {
  name                      = "${var.prefix}-${var.environment}-db"
  project                   = var.project_id
  zone                      = var.zone
  machine_type              = "e2-micro" # the Free Tier machine type; anything else is billed
  tags                      = [var.network_tag]
  allow_stopping_for_update = true
  deletion_protection       = var.deletion_protection
  labels                    = var.labels

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = 10
      type  = "pd-standard"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "pgdata"
  }

  network_interface {
    subnetwork = var.subnetwork
    network_ip = google_compute_address.internal.address
    # no access_config block: no external IP
  }

  service_account {
    email  = var.service_account
    scopes = ["cloud-platform"] # authorization is the account's IAM: two resource grants
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script = templatefile("${path.module}/startup.sh.tftpl", {
      project_id  = var.project_id
      secret_id   = var.password_secret_id
      registry    = split("/", var.postgres_image)[0]
      image       = var.postgres_image
      cidr        = var.allowed_cidr
      db_user     = var.db_user
      db_name     = var.db_name
      memory_args = "-c shared_buffers=128MB -c max_connections=40 -c work_mem=4MB"
    })
  }

  scheduling {
    on_host_maintenance = "MIGRATE"
    automatic_restart   = true
    preemptible         = false
  }

  lifecycle {
    # cos-stable resolves to a new image every release; COS updates itself in place, so a
    # new release must never force the VM (and the boot disk) to be replaced.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }
}
