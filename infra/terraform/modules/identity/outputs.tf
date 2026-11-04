output "emails" {
  description = "Service account email per process."
  value       = { for k, sa in google_service_account.service : k => sa.email }
}

output "members" {
  value = { for k, sa in google_service_account.service : k => "serviceAccount:${sa.email}" }
}

output "secret_ids" {
  value = { for k, s in google_secret_manager_secret.secret : k => s.secret_id }
}

output "secret_grants" {
  description = "secret/reader pairs (pinned by tests/security/test_terraform_posture.py)."
  value       = sort(keys(google_secret_manager_secret_iam_member.reader))
}
