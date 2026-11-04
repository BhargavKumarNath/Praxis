output "internal_ip" {
  value = google_compute_instance.db.network_interface[0].network_ip
}

output "instance" {
  value = google_compute_instance.db.name
}

output "machine_type" {
  value = google_compute_instance.db.machine_type
}

output "has_external_ip" {
  value = length(google_compute_instance.db.network_interface[0].access_config) > 0
}
