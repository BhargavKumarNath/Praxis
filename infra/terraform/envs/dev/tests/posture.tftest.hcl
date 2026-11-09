# Phase 15 gate "infrastructure can be destroyed / recreated" (ADR 0024), at GBP 0.
#
# Both providers are MOCKED: no credentials, no API call, no spend. Each `apply` run builds
# the full dev graph in Terraform's state and the test framework destroys it at the end, in
# reverse dependency order; the second apply proves a clean recreate. What this proves: the
# configuration plans, creates and destroys without cycles or blocked deletions (deletion
# protection off, force_destroy on, delete_contents_on_destroy on). What it cannot prove:
# real GCP API behaviour; that needs one owner-approved apply/destroy (docs/cost-guard.md).
mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
}

mock_provider "google-beta" {}

variables {
  project_id       = "praxis-test"
  deploy_services  = true
  billing_account  = "000000-000000-000000"
  console_invokers = ["user:owner@example.com"]
  app_image        = "us-central1-docker.pkg.dev/praxis-test/praxis/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  web_image        = "us-central1-docker.pkg.dev/praxis-test/praxis/console-web@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  postgres_image   = "us-central1-docker.pkg.dev/praxis-test/praxis/postgres@sha256:3333333333333333333333333333333333333333333333333333333333333333"
}

run "create" {
  command = apply

  assert {
    condition     = length(output.service_accounts) == 10
    error_message = "one service account per process (api, console, console-public, streaming, ops, agent, db, guard, live, scheduler)"
  }
  assert {
    condition     = !output.services.api.public && !output.services.console.public && output.services.console_public == null
    error_message = "nothing is public by default: api_public and console_public are owner opt-ins"
  }
  assert {
    condition     = output.database.machine_type == "e2-micro" && !output.database.has_external_ip
    error_message = "the database is the Free Tier e2-micro with no external IP"
  }
  assert {
    condition     = length(output.jobs) == 3
    error_message = "the ops, streaming and live jobs are declared"
  }
  assert {
    condition     = length(output.schedule) == 2 && output.schedule["live-tick"] == "10 3 * * *" && output.schedule["ops-tick"] == "50 3 * * *"
    error_message = "two daily triggers only: the live tick, then one ops tick (ADR 0027)"
  }
  assert {
    condition     = contains(output.secret_grants, "database-url/live") && length([for g in output.secret_grants : g if endswith(g, "/live")]) == 1 && length([for g in output.secret_grants : g if endswith(g, "/scheduler")]) == 0
    error_message = "the live job reads only the database URL; the scheduler reads no secret"
  }
  assert {
    condition     = output.services.api.max_instances <= 10 && output.services.console.max_instances <= 10
    error_message = "every service is capped at <= 10 instances"
  }
  assert {
    condition     = module.api[0].max_instances == 2 && module.console[0].max_instances == 1
    error_message = "dev caps: API 2, console 1"
  }
  assert {
    condition     = output.cost_controls.budget_gbp == 10 && output.cost_controls.alert_points_gbp == [2, 5, 8, 10]
    error_message = "the GBP 10 budget alerts at GBP 2 / 5 / 8 / 10"
  }
  assert {
    condition     = output.cost_controls.spend_guard_gbp == 8
    error_message = "the spend guard unlinks billing at GBP 8, before the ceiling"
  }
  assert {
    condition     = output.cost_controls.bigquery_daily_query_mib * 31 <= 1048576
    error_message = "the BigQuery daily cap stays inside the 1 TiB monthly free tier"
  }
  assert {
    condition     = !contains(output.secret_grants, "stripe-webhook-secret/console") && !contains(output.secret_grants, "console-approvers/api") && !contains(output.secret_grants, "stripe-api-key/agent")
    error_message = "secrets are isolated per process"
  }
  assert {
    condition     = contains(output.secret_grants, "postgres-password/db") && length([for g in output.secret_grants : g if startswith(g, "postgres-password/")]) == 1
    error_message = "only the database VM reads the Postgres password"
  }
}

run "the_api_can_be_opened_for_stripe" {
  command = plan

  variables {
    api_public     = true
    stripe_enabled = true
  }

  assert {
    condition     = output.services.api.public && !output.services.console.public
    error_message = "api_public opens the API only, never the console"
  }
}

run "the_read_only_console_copy_can_be_public" {
  command = plan

  variables {
    console_public = true
  }

  assert {
    condition     = output.services.console_public.public && !output.services.console.public && !output.services.api.public
    error_message = "console_public opens only the read-only copy, never the approving console or the API"
  }
  assert {
    condition     = output.services.console_public.max_instances == 1
    error_message = "the public console copy is capped at one instance"
  }
  assert {
    condition     = !contains(output.secret_grants, "console-approvers/console-public") && contains(output.secret_grants, "database-url/console-public") && length([for g in output.secret_grants : g if endswith(g, "/console-public")]) == 1
    error_message = "the public copy reads only the database URL, never the approver secret"
  }
}

run "recreate_after_destroy" {
  command = apply

  variables {
    deploy_services = false
    billing_account = null
  }

  assert {
    condition     = output.services.api == null && output.database == null && output.cost_controls.budget_gbp == null && output.cost_controls.spend_guard_gbp == null
    error_message = "the runtime, budget and spend guard are removed cleanly and the rest recreates"
  }
}

run "console_can_never_be_public" {
  command = plan

  module {
    source = "../../modules/cloud_run"
  }

  variables {
    environment     = "dev"
    location        = "europe-west2"
    name            = "console"
    image           = "img"
    service_account = "sa@example.com"
    public          = true
  }

  expect_failures = [var.public]
}

run "max_instances_is_capped" {
  command = plan

  module {
    source = "../../modules/cloud_run"
  }

  variables {
    environment     = "dev"
    location        = "europe-west2"
    name            = "api"
    image           = "img"
    service_account = "sa@example.com"
    max_instances   = 11
  }

  expect_failures = [var.max_instances]
}

run "budget_cannot_exceed_gbp_10" {
  command = plan

  module {
    source = "../../modules/cost_controls"
  }

  variables {
    environment        = "dev"
    monthly_budget_gbp = 11
  }

  expect_failures = [var.monthly_budget_gbp]
}

run "database_must_be_free_tier_region" {
  command = plan

  module {
    source = "../../modules/database"
  }

  variables {
    environment        = "dev"
    region             = "europe-west2"
    zone               = "europe-west2-a"
    subnetwork         = "s"
    allowed_cidr       = "10.10.0.0/24"
    internal_ip        = "10.10.0.10"
    network_tag        = "praxis-db"
    service_account    = "sa@example.com"
    password_secret_id = "p"
    postgres_image     = "us-central1-docker.pkg.dev/p/praxis/postgres@sha256:3333333333333333333333333333333333333333333333333333333333333333"
  }

  expect_failures = [var.region]
}

run "spend_guard_acts_below_the_ceiling" {
  command = plan

  module {
    source = "../../modules/spend_guard"
  }

  variables {
    environment         = "dev"
    location            = "us-central1"
    image               = "img"
    service_account     = "sa@example.com"
    budget_display_name = "praxis-dev-monthly"
    kill_at_gbp         = 12
  }

  expect_failures = [var.kill_at_gbp]
}

run "bigquery_cap_stays_in_the_free_tier" {
  command = plan

  module {
    source = "../../modules/cost_controls"
  }

  variables {
    environment              = "dev"
    bigquery_daily_query_mib = 40000
  }

  expect_failures = [var.bigquery_daily_query_mib]
}

run "schedule_runs_at_most_daily" {
  command = plan

  module {
    source = "../../modules/schedule"
  }

  variables {
    environment     = "dev"
    location        = "us-central1"
    service_account = "sa@example.com"
    jobs = {
      hourly = { job = "j", cron = "10 * * * *", description = "every hour" }
    }
  }

  expect_failures = [var.jobs]
}
