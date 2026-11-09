output "raw_bucket_name" {
  value = module.storage.raw_bucket_name
}

output "events_topic" {
  value = module.pubsub.events_topic
}

output "event_subscriptions" {
  value = module.pubsub.subscriptions
}

output "bigquery_datasets" {
  value = module.bigquery.dataset_ids
}

output "service_accounts" {
  value = module.identity.emails
}

output "secret_grants" {
  value = module.identity.secret_grants
}

output "repository_url" {
  value = module.registry.repository_url
}

output "enabled_apis" {
  value = module.apis.services
}

output "database" {
  value = var.deploy_services ? {
    instance        = module.database[0].instance
    internal_ip     = module.database[0].internal_ip
    machine_type    = module.database[0].machine_type
    has_external_ip = module.database[0].has_external_ip
  } : null
}

output "services" {
  value = {
    api            = var.deploy_services ? { uri = module.api[0].uri, public = module.api[0].public, max_instances = module.api[0].max_instances } : null
    console        = var.deploy_services ? { uri = module.console[0].uri, public = module.console[0].public, max_instances = module.console[0].max_instances } : null
    console_public = length(module.console_public) > 0 ? { uri = module.console_public[0].uri, public = module.console_public[0].public, max_instances = module.console_public[0].max_instances } : null
  }
}

output "jobs" {
  value = var.deploy_services ? [module.ops_job[0].name, module.streaming_job[0].name, module.live_job[0].name] : []
}

output "live_bucket" {
  value = module.storage.live_bucket
}

output "schedule" {
  value = var.deploy_services ? module.schedule[0].triggers : {}
}

output "cost_controls" {
  value = {
    budget_gbp               = module.cost_controls.budget_gbp
    alert_points_gbp         = module.cost_controls.alert_points_gbp
    spend_guard_gbp          = length(module.spend_guard) > 0 ? module.spend_guard[0].kill_at_gbp : null
    bigquery_daily_query_mib = module.cost_controls.bigquery_daily_query_mib
  }
}
