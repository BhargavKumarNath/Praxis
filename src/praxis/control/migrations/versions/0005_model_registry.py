"""Model registry, lifecycle audit, monitoring reports and retraining requests (Phase 10).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

ADR 0017. The control plane is the AUTHORITATIVE registry: which version is champion is a
row here, never only an MLflow alias (MLflow mirrors it for experiment browsing).

* ``model_versions``: one row per registered artifact with its full lineage (data,
  feature and code versions, parameters, metrics, checksum, training time). Lineage columns
  are immutable after insert; only the stage (and bookkeeping) may change. ``stage`` moves
  only along the direct edges of ``praxis.domain.model_lifecycle`` (seeded below; a test
  keeps them equal). Partial unique indexes: at most one champion, one shadow and one
  canary per model name, so "two champions" is unrepresentable.
* ``model_stage_events``: append-only audit of every move. A promotion or rollback writes
  both versions' moves under one ``decision_id`` in one transaction.
* ``model_validation_reports``: append-only offline validation results (every check).
* ``model_monitoring_reports``: append-only drift / shadow / probation reports; the id is a
  content hash of (version, kind, as-of), so re-running a monitor is idempotent.
* ``retraining_requests``: drift, schedule or new labels ask for a candidate. At most one
  live (open / training) request per model: repeated alarms join it instead of piling up.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ALLOWED_MOVES = {
    "model": [
        ("canary", "champion"), ("canary", "rejected"), ("candidate", "challenger"),
        ("candidate", "champion"), ("candidate", "rejected"), ("challenger", "champion"),
        ("challenger", "rejected"), ("challenger", "shadow"), ("champion", "retired"),
        ("champion", "rolled_back"), ("retired", "champion"), ("shadow", "canary"),
        ("shadow", "champion"), ("shadow", "rejected"),
    ],
    "retraining": [
        ("open", "failed"), ("open", "training"), ("training", "completed"),
        ("training", "failed"),
    ],
}  # fmt: skip

SCHEMA = """
-- Generic guard for a state column with another name: TG_ARGV = (machine, column).
CREATE FUNCTION guard_column_move() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_value TEXT := to_jsonb(OLD) ->> TG_ARGV[1];
    new_value TEXT := to_jsonb(NEW) ->> TG_ARGV[1];
BEGIN
    IF old_value IS DISTINCT FROM new_value AND NOT EXISTS (
        SELECT 1 FROM allowed_state_moves
        WHERE machine = TG_ARGV[0] AND from_state = old_value AND to_state = new_value
    ) THEN
        RAISE EXCEPTION 'forbidden % move % -> %', TG_ARGV[0], old_value, new_value
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE model_versions (
    model_version     TEXT        PRIMARY KEY,
    model_name        TEXT        NOT NULL CHECK (length(model_name) > 0),
    stage             TEXT        NOT NULL CHECK (stage IN (
        'candidate', 'challenger', 'shadow', 'canary', 'champion', 'retired', 'rejected',
        'rolled_back')),
    artifact_uri      TEXT        NOT NULL CHECK (length(artifact_uri) > 0),
    artifact_checksum TEXT        NOT NULL CHECK (artifact_checksum ~ '^[0-9a-f]{64}$'),
    data_version      TEXT        NOT NULL CHECK (length(data_version) > 0),
    feature_version   TEXT        NOT NULL CHECK (length(feature_version) > 0),
    code_revision     TEXT        NOT NULL CHECK (length(code_revision) > 0),
    params            JSONB       NOT NULL,
    metrics           JSONB       NOT NULL,
    lineage           JSONB       NOT NULL,
    reference         JSONB,      -- monitoring reference profile (bins, metric levels)
    trained_at        TIMESTAMPTZ NOT NULL,
    is_synthetic      BOOLEAN     NOT NULL,
    tracking_run_id   TEXT,       -- MLflow run (mirror); NULL = not mirrored yet
    registered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage_changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    version           INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX model_versions_name_stage ON model_versions (model_name, stage);
CREATE UNIQUE INDEX model_versions_one_champion ON model_versions (model_name)
    WHERE stage = 'champion';
CREATE UNIQUE INDEX model_versions_one_shadow ON model_versions (model_name)
    WHERE stage = 'shadow';
CREATE UNIQUE INDEX model_versions_one_canary ON model_versions (model_name)
    WHERE stage = 'canary';
CREATE TRIGGER model_versions_stage_guard BEFORE UPDATE ON model_versions
    FOR EACH ROW EXECUTE FUNCTION guard_column_move('model', 'stage');

CREATE FUNCTION freeze_model_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.model_name, OLD.artifact_uri, OLD.artifact_checksum, OLD.data_version,
        OLD.feature_version, OLD.code_revision, OLD.params, OLD.metrics, OLD.lineage,
        OLD.trained_at, OLD.is_synthetic)
       IS DISTINCT FROM
       (NEW.model_name, NEW.artifact_uri, NEW.artifact_checksum, NEW.data_version,
        NEW.feature_version, NEW.code_revision, NEW.params, NEW.metrics, NEW.lineage,
        NEW.trained_at, NEW.is_synthetic) THEN
        RAISE EXCEPTION 'model lineage of % is immutable', OLD.model_version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER model_versions_lineage_frozen BEFORE UPDATE ON model_versions
    FOR EACH ROW EXECUTE FUNCTION freeze_model_lineage();
CREATE TRIGGER model_versions_no_delete BEFORE DELETE ON model_versions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE model_stage_events (
    event_id      BIGSERIAL   PRIMARY KEY,
    decision_id   TEXT        NOT NULL,
    model_name    TEXT        NOT NULL,
    model_version TEXT        NOT NULL REFERENCES model_versions (model_version),
    from_stage    TEXT,       -- NULL = registration
    to_stage      TEXT        NOT NULL CHECK (to_stage IN (
        'candidate', 'challenger', 'shadow', 'canary', 'champion', 'retired', 'rejected',
        'rolled_back')),
    trigger       TEXT        NOT NULL,
    actor         TEXT        NOT NULL CHECK (length(actor) > 0),
    reason        TEXT        NOT NULL CHECK (length(reason) > 0),
    evidence      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    trace_id      TEXT        NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX model_stage_events_version ON model_stage_events (model_version, event_id);
CREATE INDEX model_stage_events_decision ON model_stage_events (decision_id);
CREATE TRIGGER model_stage_events_append_only BEFORE UPDATE OR DELETE ON model_stage_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE model_validation_reports (
    report_id     TEXT        PRIMARY KEY,
    model_version TEXT        NOT NULL REFERENCES model_versions (model_version),
    passed        BOOLEAN     NOT NULL,
    failed_checks TEXT[]      NOT NULL,
    report        JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (passed = (cardinality(failed_checks) = 0))
);
CREATE INDEX model_validation_reports_version ON model_validation_reports (model_version);
CREATE TRIGGER model_validation_reports_append_only BEFORE UPDATE OR DELETE
    ON model_validation_reports FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE model_monitoring_reports (
    report_id     TEXT        PRIMARY KEY,
    model_name    TEXT        NOT NULL,
    model_version TEXT        NOT NULL REFERENCES model_versions (model_version),
    kind          TEXT        NOT NULL CHECK (kind IN ('drift', 'shadow', 'probation')),
    as_of         DATE        NOT NULL,
    window_start  DATE        NOT NULL,
    window_end    DATE        NOT NULL,
    fired         BOOLEAN     NOT NULL,
    signals       JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_start <= window_end)
);
CREATE INDEX model_monitoring_reports_version ON model_monitoring_reports (model_version, as_of);
CREATE TRIGGER model_monitoring_reports_append_only BEFORE UPDATE OR DELETE
    ON model_monitoring_reports FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE retraining_requests (
    request_id        TEXT        PRIMARY KEY,
    model_name        TEXT        NOT NULL,
    status            TEXT        NOT NULL
        CHECK (status IN ('open', 'training', 'completed', 'failed')),
    triggers          TEXT[]      NOT NULL CHECK (cardinality(triggers) > 0),
    evidence          JSONB       NOT NULL,
    as_of             DATE        NOT NULL,      -- logical (data) date of the request
    source_report_id  TEXT        REFERENCES model_monitoring_reports (report_id),
    champion_version  TEXT        REFERENCES model_versions (model_version),
    candidate_version TEXT        REFERENCES model_versions (model_version),
    failure_reason    TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'completed') = (candidate_version IS NOT NULL)),
    CHECK ((status = 'failed') = (failure_reason IS NOT NULL))
);
CREATE UNIQUE INDEX retraining_requests_one_live ON retraining_requests (model_name)
    WHERE status IN ('open', 'training');
CREATE TRIGGER retraining_requests_status_guard BEFORE UPDATE ON retraining_requests
    FOR EACH ROW EXECUTE FUNCTION guard_column_move('retraining', 'status');
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    rows = ", ".join(
        f"('{machine}', '{src}', '{dst}')"
        for machine, moves in ALLOWED_MOVES.items()
        for src, dst in moves
    )
    op.execute(f"INSERT INTO allowed_state_moves VALUES {rows}")  # noqa: S608 - static literals


def downgrade() -> None:
    op.execute(
        "DROP TABLE retraining_requests, model_monitoring_reports, model_validation_reports, "
        "model_stage_events, model_versions; "
        "DROP FUNCTION freeze_model_lineage(); DROP FUNCTION guard_column_move(); "
        "DELETE FROM allowed_state_moves WHERE machine IN ('model', 'retraining');"
    )
