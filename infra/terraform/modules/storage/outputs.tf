output "raw_bucket_name" {
  value = google_storage_bucket.raw.name
}

output "live_bucket" {
  value = google_storage_bucket.live.name
}
