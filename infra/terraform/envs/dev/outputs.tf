output "raw_bucket_name" {
  value = module.storage.raw_bucket_name
}

output "events_topic" {
  value = module.pubsub.events_topic
}

output "bigquery_datasets" {
  value = module.bigquery.dataset_ids
}
