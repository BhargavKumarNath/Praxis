"""Phase 15 static security / cost posture of ``infra/terraform`` (ADR 0024).

Complements ``make tf-test`` (mocked plan / apply / destroy): these checks read the HCL so a
regression fails ``make test`` even without Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

from praxis.config import Settings

ROOT = Path(__file__).resolve().parents[2] / "infra" / "terraform"
HCL = {p: p.read_text() for p in ROOT.rglob("*.tf") if ".terraform" not in p.parts}


def _all() -> str:
    return "\n".join(HCL.values())


def test_no_project_level_or_primitive_roles() -> None:
    """One classified exception (ADR 0026): the spend guard's roles/billing.projectManager,
    the narrowest role that can unlink billing, granted to the guard's own account only."""
    grants = {
        p.relative_to(ROOT).as_posix(): re.findall(
            r'resource\s+"google_project_iam_(member|binding|policy)"', t)
        for p, t in HCL.items()
    }  # fmt: skip
    assert {p: g for p, g in grants.items() if g} == {"modules/spend_guard/main.tf": ["member"]}
    guard = (ROOT / "modules/spend_guard/main.tf").read_text()
    block = guard[guard.index('resource "google_project_iam_member"') :]
    block = block.split("\n}")[0]
    assert 'role    = "roles/billing.projectManager"' in block
    assert 'member  = "serviceAccount:${var.service_account}"' in block
    text = _all()
    assert not re.search(r'roles/(owner|editor|viewer)"', text)
    assert "google_service_account_key" not in text  # no exported keys, ever


def test_only_the_api_cloud_run_module_can_be_public() -> None:
    hits = [p for p, t in HCL.items() if '"allUsers"' in t or "allAuthenticatedUsers" in t]
    # the cloud_run module holds the single allUsers grant (and the validation refusing it)
    assert {p.relative_to(ROOT).as_posix() for p in hits} <= {
        "modules/cloud_run/main.tf", "modules/cloud_run/variables.tf"}  # fmt: skip
    dev = (ROOT / "envs/dev/main.tf").read_text()
    # The API is private by default; api_public opens it for the Stripe webhook only.
    assert dev.count("public          = var.api_public") == 1
    assert "public          = true" not in dev
    variables = (ROOT / "envs/dev/variables.tf").read_text()
    assert "default     = false" in variables.split('variable "api_public"')[1].split("}")[0]
    console = dev[dev.index('module "console"') :]
    assert "public " not in console.split("labels")[0]
    # ADR 0032: the only other public service is the read-only console copy, opt-in and
    # off by default, and it never receives the approver secret.
    copy = dev[dev.index('module "console_public"') :].split("labels")[0]
    assert "public          = var.console_public" in copy
    assert "console-approvers" not in copy and "PRAXIS_CONSOLE_APPROVERS" not in copy
    assert "default     = false" in variables.split('variable "console_public"')[1].split("}")[0]


def test_services_scale_to_zero_with_a_cap() -> None:
    run = (ROOT / "modules/cloud_run/main.tf").read_text()
    assert "min_instance_count = 0" in run
    assert "cpu_idle          = true" in run  # request-based billing
    assert "var.max_instances <= 10" in (ROOT / "modules/cloud_run/variables.tf").read_text()


def test_no_secret_value_is_in_terraform() -> None:
    text = _all()
    assert "google_secret_manager_secret_version" not in text  # values added out of band
    assert not re.search(r"(sk_live|sk_test|whsec_)[A-Za-z0-9]", text)


def test_cloud_run_env_names_are_real_settings() -> None:
    names = set(re.findall(r"\b(PRAXIS_[A-Z_]+)\s*=", (ROOT / "envs/dev/main.tf").read_text()))
    fields = {f"PRAXIS_{n.upper()}" for n in Settings.model_fields}
    assert names and names <= fields, names - fields


def test_dev_is_destroyable() -> None:
    dev = (ROOT / "envs/dev/main.tf").read_text()
    assert "force_destroy = true" in dev
    assert "delete_contents_on_destroy = true" in dev
    assert "default     = false" in (ROOT / "modules/cloud_run/variables.tf").read_text().split(
        'variable "deletion_protection"')[1]  # fmt: skip


def test_no_external_ip_nat_or_load_balancer() -> None:
    """ADR 0026: nothing billable or exposed by the network itself."""
    text = _all()
    assert not re.search(r"access_config\s*\{", text)  # no VM external IP block
    for kind in ("google_compute_router_nat", "google_compute_global_forwarding_rule",
                 "google_compute_forwarding_rule", "google_compute_global_address",
                 "google_sql_database_instance"):  # fmt: skip
        assert kind not in text, kind
    assert 'address_type = "INTERNAL"' in (ROOT / "modules/database/main.tf").read_text()


def test_database_is_free_tier_and_private() -> None:
    db = (ROOT / "modules/database/main.tf").read_text()
    assert 'machine_type              = "e2-micro"' in db
    assert 'enable-oslogin         = "TRUE"' in db
    fw = (ROOT / "modules/network/main.tf").read_text()
    assert "0.0.0.0/0" not in fw
    assert fw.count("source_ranges") == 2  # the subnet (5432) and Google IAP (22) only


def test_live_volume_is_private_and_least_privilege() -> None:
    """ADR 0027: only the live job writes the live bucket; four processes read it (ADR 0032
    added the read-only public console)."""
    storage = (ROOT / "modules/storage/main.tf").read_text()
    live = storage[storage.index('resource "google_storage_bucket" "live"') :]
    assert 'public_access_prevention    = "enforced"' in live.split("\n}")[0]
    assert "uniform_bucket_level_access = true" in live.split("\n}")[0]
    roles = re.findall(r'role\s+=\s+"(roles/[^"]+)"', live)
    assert roles == ["roles/storage.objectUser", "roles/storage.objectViewer"]
    dev = (ROOT / "envs/dev/main.tf").read_text()
    block = dev[dev.index("live_writers") : dev.index("labels     = local.labels")]
    assert block.count("module.identity.members") == 5
    assert 'live_writers  = { live = module.identity.members["live"] }' in block
    for reader in ("api", "console", "ops", "console-public"):
        assert re.search(rf'{reader}\s+= module.identity.members\["{reader}"\]', block)
    # every reader mounts it read-only; only the live job mounts it read-write
    assert dev.count("gcs_volume      = local.live_ro") == 4
    assert dev.count("gcs_volume      = local.live_rw") == 1


def test_scheduler_can_only_start_two_jobs_daily() -> None:
    """The scheduler identity holds roles/run.invoker on the live and ops jobs only, and no
    secret; each trigger fires at most once a day without retries."""
    dev = (ROOT / "envs/dev/main.tf").read_text()
    assert dev.count('invokers        = { scheduler = module.identity.members["scheduler"] }') == 2
    job = (ROOT / "modules/cloud_run_job/main.tf").read_text()
    assert 'role     = "roles/run.invoker"' in job
    schedule = (ROOT / "modules/schedule/main.tf").read_text()
    assert "retry_count = 0" in schedule
    assert "oauth_token" in schedule and ":run" in schedule
    assert "length(var.jobs) <= 3" in (ROOT / "modules/schedule/variables.tf").read_text()
    identity = (ROOT / "modules/identity/main.tf").read_text()
    readers = identity[identity.index("secret_readers = {") : identity.index("grants = merge")]
    assert "scheduler" not in readers
    assert readers.count('"live"') == 1  # the live job reads the database URL only
