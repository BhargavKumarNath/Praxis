# Private network for the control plane (deployment, ADR 0026).
#
# No external IP, NAT or load balancer exists anywhere (each would be a recurring charge and
# an exposure). Cloud Run reaches Postgres by Direct VPC egress into this subnet; the
# database VM reaches Google APIs (Secret Manager, Artifact Registry) by Private Google
# Access. Ingress is denied by default; two rules open exactly Postgres from this subnet and
# SSH from Google's IAP TCP-forwarding range.
resource "google_compute_network" "vpc" {
  name                    = "${var.prefix}-${var.environment}"
  project                 = var.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "private" {
  name                     = "${var.prefix}-${var.environment}-${var.region}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.cidr
  private_ip_google_access = true

  # Flow logs (trivy GCP-0076). Private subnet only, so volume is KBs a month, inside the
  # Logging free 50 GiB / project; 10 min aggregation + 50 % sampling keep it that way.
  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "EXCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "postgres" {
  name          = "${var.prefix}-${var.environment}-allow-postgres"
  project       = var.project_id
  network       = google_compute_network.vpc.id
  direction     = "INGRESS"
  source_ranges = [var.cidr]
  target_tags   = [var.db_tag]

  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

# Maintenance only: `gcloud compute ssh --tunnel-through-iap` (OS Login, IAM-checked).
resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.prefix}-${var.environment}-allow-iap-ssh"
  project       = var.project_id
  network       = google_compute_network.vpc.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [var.db_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
