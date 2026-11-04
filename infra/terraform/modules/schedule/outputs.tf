output "triggers" {
  value = { for k, j in google_cloud_scheduler_job.job : k => j.schedule }
}
