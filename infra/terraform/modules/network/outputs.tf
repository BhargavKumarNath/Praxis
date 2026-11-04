output "network" {
  value = google_compute_network.vpc.id
}

output "subnetwork" {
  value = google_compute_subnetwork.private.id
}

output "cidr" {
  value = var.cidr
}

output "db_tag" {
  value = var.db_tag
}
