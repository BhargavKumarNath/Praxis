# The deployed Praxis environment (ADR 0024, ADR 0026). `make tf-test` plans, applies and
# destroys this whole graph against MOCKED providers at GBP 0; a real apply is an owner action
# (docs/deployment.md). Region: compute in us-central1 (the Free Tier e2-micro, Cloud Storage
# and Cloud Run tier-1 region); the Phase 2 BigQuery datasets stay in europe-west2.
provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  environment = "dev"
  zone        = "${var.region}-a"
  labels = {
    app         = "praxis"
    environment = local.environment
    managed_by  = "terraform"
  }
  budget_name = "praxis-${local.environment}-monthly"
  vpc_egress  = { network = module.network.network, subnetwork = module.network.subnetwork }
  common_env  = { PRAXIS_ENVIRONMENT = local.environment, PRAXIS_GCP_PROJECT_ID = var.project_id }
  # The live volume (ADR 0027): the live job writes the SYNTHETIC warehouse, model artifacts
  # and tick reports; the API, console and ops monitor mount it read-only at the same path,
  # so a registered artifact's file:// URI resolves in every process.
  live_mount = "/mnt/live"
  live_env = {
    PRAXIS_LIVE_ROOT      = local.live_mount
    PRAXIS_WAREHOUSE_PATH = "${local.live_mount}/warehouse/praxis.duckdb"
  }
  live_ro = { bucket = module.storage.live_bucket, mount_path = local.live_mount, read_only = true }
  # The writer mounts as the image's non-root user (uid / gid 10001); a mount is root-owned
  # otherwise. Read-only mounts stay root-owned and world-readable.
  live_rw = {
    bucket        = module.storage.live_bucket
    mount_path    = local.live_mount
    read_only     = false
    mount_options = ["uid=10001", "gid=10001"]
  }
}

module "apis" {
  source     = "../../modules/project_services"
  project_id = var.project_id
}

module "network" {
  source      = "../../modules/network"
  project_id  = var.project_id
  environment = local.environment
  region      = var.region
  depends_on  = [module.apis]
}

module "registry" {
  source         = "../../modules/registry"
  project_id     = var.project_id
  location       = var.region
  reader_members = { db = module.identity.members["db"] }
  labels         = local.labels
  depends_on     = [module.apis]
}

module "storage" {
  source        = "../../modules/storage"
  project_id    = var.project_id
  environment   = local.environment
  location      = var.region
  force_destroy = true
  live_writers  = { live = module.identity.members["live"] }
  live_readers = {
    api            = module.identity.members["api"]
    console        = module.identity.members["console"]
    ops            = module.identity.members["ops"]
    console-public = module.identity.members["console-public"]
  }
  labels     = local.labels
  depends_on = [module.apis]
}

module "pubsub" {
  source            = "../../modules/pubsub"
  project_id        = var.project_id
  environment       = local.environment
  labels            = local.labels
  publisher_members = { api = module.identity.members["api"], streaming = module.identity.members["streaming"] }
  subscriber_members = {
    operational = module.identity.members["streaming"]
    warehouse   = module.identity.members["streaming"]
    monitoring  = module.identity.members["streaming"]
    dunning     = module.identity.members["streaming"]
    dlq_inspect = module.identity.members["ops"]
  }
  dead_letter_publisher_members = { streaming = module.identity.members["streaming"] }
  depends_on                    = [module.apis]
}

module "bigquery" {
  source                     = "../../modules/bigquery"
  project_id                 = var.project_id
  environment                = local.environment
  location                   = var.bigquery_location
  delete_contents_on_destroy = true
  labels                     = merge(local.labels, { data = "synthetic_and_public" })
  depends_on                 = [module.apis]
}

module "tasks" {
  source           = "../../modules/tasks"
  project_id       = var.project_id
  environment      = local.environment
  location         = var.region
  enqueuer_members = { api = module.identity.members["api"] }
  depends_on       = [module.apis]
}

module "identity" {
  source      = "../../modules/identity"
  project_id  = var.project_id
  environment = local.environment
  location    = var.region
  labels      = local.labels
  depends_on  = [module.apis]
}

module "cost_controls" {
  source             = "../../modules/cost_controls"
  project_id         = var.project_id
  environment        = local.environment
  billing_account    = var.billing_account
  monthly_budget_gbp = var.monthly_budget_gbp
  notification_topic = length(module.spend_guard) > 0 ? module.spend_guard[0].topic : null
  providers = {
    google      = google
    google-beta = google-beta
  }
  depends_on = [module.apis]
}

# --- runtime: declared only with deploy_services = true (needs pushed images) -------------

module "database" {
  count              = var.deploy_services ? 1 : 0
  source             = "../../modules/database"
  project_id         = var.project_id
  environment        = local.environment
  region             = var.region
  zone               = local.zone
  subnetwork         = module.network.subnetwork
  allowed_cidr       = module.network.cidr
  internal_ip        = cidrhost(module.network.cidr, 10)
  network_tag        = module.network.db_tag
  service_account    = module.identity.emails["db"]
  password_secret_id = module.identity.secret_ids["postgres-password"]
  postgres_image     = var.postgres_image
  labels             = local.labels
}

# The API serves forecasts, ops and (when public) Stripe webhooks. It stays PRIVATE until
# the Stripe integration is switched on (api_public): IAM-denied requests are not billed,
# so a private API has no anonymous cost exposure.
module "api" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/cloud_run"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "api"
  image           = var.app_image
  service_account = module.identity.emails["api"]
  public          = var.api_public
  invokers        = var.console_invokers
  memory          = "1Gi"
  vpc_egress      = local.vpc_egress
  gcs_volume      = local.live_ro
  # Serves the registry's champion (ADR 0017): a promotion or rollback is picked up within
  # model_refresh_interval_s, with no redeploy.
  env = merge(local.common_env, local.live_env, {
    PRAXIS_SERVICE_NAME           = "praxis-api"
    PRAXIS_FORECAST_FROM_REGISTRY = "true"
  })
  # Webhook / retry-task secrets are wired when their values exist (Stripe integration);
  # until then those routes answer 503 by design.
  secret_env = merge(
    { PRAXIS_DATABASE_URL = module.identity.secret_ids["database-url"] },
    var.stripe_enabled ? {
      PRAXIS_STRIPE_WEBHOOK_SECRET = module.identity.secret_ids["stripe-webhook-secret"]
      PRAXIS_TASKS_TOKEN           = module.identity.secret_ids["task-token"]
    } : {},
  )
  labels = local.labels
}

# The console: the Next UI (ingress) with the console API as a loopback-only sidecar.
module "console" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/cloud_run"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "console"
  image           = var.web_image
  service_account = module.identity.emails["console"]
  max_instances   = 1
  memory          = "512Mi"
  invokers        = var.console_invokers
  vpc_egress      = local.vpc_egress
  gcs_volume      = local.live_ro
  env             = merge(local.common_env, local.live_env, { PRAXIS_SERVICE_NAME = "praxis-console" })
  sidecars = [{
    name    = "console-api"
    image   = var.app_image
    port    = 8001
    command = ["uvicorn"]
    # 0.0.0.0 so Cloud Run's startup probe reaches it; only the ingress container's port is
    # routable from outside the instance, so the sidecar stays private.
    args   = ["--factory", "praxis.console.app:create_console_app", "--host", "0.0.0.0", "--port", "8001"]
    memory = "1Gi"
    secret_env = {
      PRAXIS_DATABASE_URL      = module.identity.secret_ids["database-url"]
      PRAXIS_CONSOLE_APPROVERS = module.identity.secret_ids["console-approvers"]
    }
  }]
  labels = local.labels
}

# A public, read-only copy of the console (ADR 0032). Same images; no approver secret, so the
# console API's admin routes answer 503 and nobody can approve, reject or lift a freeze through
# it. Viewing is open to anyone. Capped at one instance; scales to zero.
module "console_public" {
  count           = var.deploy_services && var.console_public ? 1 : 0
  source          = "../../modules/cloud_run"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "console-public"
  image           = var.web_image
  service_account = module.identity.emails["console-public"]
  max_instances   = 1
  memory          = "512Mi"
  public          = var.console_public
  vpc_egress      = local.vpc_egress
  gcs_volume      = local.live_ro
  env             = merge(local.common_env, local.live_env, { PRAXIS_SERVICE_NAME = "praxis-console-public" })
  sidecars = [{
    name    = "console-api"
    image   = var.app_image
    port    = 8001
    command = ["uvicorn"]
    args    = ["--factory", "praxis.console.app:create_console_app", "--host", "0.0.0.0", "--port", "8001"]
    memory  = "1Gi"
    secret_env = {
      PRAXIS_DATABASE_URL = module.identity.secret_ids["database-url"]
    }
  }]
  labels = local.labels
}

# Bounded, on-demand work. Override the arguments per execution, e.g.
#   gcloud run jobs execute praxis-dev-ops --args=praxis.streaming,migrate --wait
module "ops_job" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/cloud_run_job"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "ops"
  image           = var.app_image
  service_account = module.identity.emails["ops"]
  command         = ["python", "-m"]
  args            = ["praxis.ops", "run", "--max-ticks", "1"]
  timeout_s       = 300
  vpc_egress      = local.vpc_egress
  gcs_volume      = local.live_ro
  env             = merge(local.common_env, local.live_env, { PRAXIS_SERVICE_NAME = "praxis-ops" })
  secret_env      = { PRAXIS_DATABASE_URL = module.identity.secret_ids["database-url"] }
  invokers        = { scheduler = module.identity.members["scheduler"] }
  labels          = local.labels
}

# The live tick (ADR 0027): rebuild the SYNTHETIC world through yesterday, publish the
# warehouse, run the model lifecycle (drift, retraining, validation, shadow, probation /
# rollback) and file a pending promotion request for a passed shadow. Promotion itself is
# always a named human's, in the console. One execution a day (schedule below), 2 vCPU /
# 4 GiB for at most 30 min: measured ~1-6 min, so ~USD 0.1-0.7 / month outside the free tier.
module "live_job" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/cloud_run_job"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "live"
  image           = var.app_image
  service_account = module.identity.emails["live"]
  command         = ["python", "-m"]
  args            = ["praxis.live", "tick"]
  timeout_s       = 14400 # retrain cost grows with history (ADR 0027 amendments 1, 2)
  cpu             = "4"   # = num_threads in configs/forecast/demand.toml
  memory          = "8Gi"
  vpc_egress      = local.vpc_egress
  gcs_volume      = local.live_rw
  env             = merge(local.common_env, local.live_env, { PRAXIS_SERVICE_NAME = "praxis-live" })
  secret_env      = { PRAXIS_DATABASE_URL = module.identity.secret_ids["database-url"] }
  invokers        = { scheduler = module.identity.members["scheduler"] }
  labels          = local.labels
}

# Once a day: the live tick at 03:10 UTC, then one ops-monitor tick at 03:50 UTC that sees
# the lifecycle's metric snapshot (drift / calibration alerts, ADR 0020).
module "schedule" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/schedule"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  service_account = module.identity.emails["scheduler"]
  jobs = {
    live-tick = { job = module.live_job[0].name, cron = "10 3 * * *", description = "Live tick: one simulated day + model lifecycle (ADR 0027)" }
    ops-tick  = { job = module.ops_job[0].name, cron = "50 3 * * *", description = "One ops-monitor tick after the live tick (ADR 0020)" }
  }
  depends_on = [module.apis]
}

module "streaming_job" {
  count           = var.deploy_services ? 1 : 0
  source          = "../../modules/cloud_run_job"
  project_id      = var.project_id
  environment     = local.environment
  location        = var.region
  name            = "streaming"
  image           = var.app_image
  service_account = module.identity.emails["streaming"]
  command         = ["python", "-m"]
  args = ["praxis.streaming", "--environment", local.environment, "consume", "--cloud",
  "--project", var.project_id, "--role", "operational", "--idle-timeout", "20"]
  timeout_s  = 600
  vpc_egress = local.vpc_egress
  env        = merge(local.common_env, { PRAXIS_SERVICE_NAME = "praxis-streaming" })
  secret_env = { PRAXIS_DATABASE_URL = module.identity.secret_ids["database-url"] }
  labels     = local.labels
}

module "spend_guard" {
  count               = var.deploy_services && var.billing_account != null ? 1 : 0
  source              = "../../modules/spend_guard"
  project_id          = var.project_id
  environment         = local.environment
  location            = var.region
  image               = var.app_image
  service_account     = module.identity.emails["guard"]
  kill_at_gbp         = var.spend_guard_gbp
  budget_display_name = local.budget_name
  labels              = local.labels
}
