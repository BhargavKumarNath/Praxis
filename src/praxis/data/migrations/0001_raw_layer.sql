-- Raw layer of the local warehouse (DuckDB). Mirrors the BigQuery raw/staging contract.
-- All timestamps are TIMESTAMPTZ and the session time zone is pinned to UTC.

CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE raw.ingest_batches (
    batch_id             VARCHAR PRIMARY KEY,
    source               VARCHAR NOT NULL,
    series_id            VARCHAR NOT NULL,
    endpoint             VARCHAR NOT NULL,
    retrieved_at         TIMESTAMPTZ NOT NULL,
    source_timestamp_min TIMESTAMPTZ,
    source_timestamp_max TIMESTAMPTZ,
    schema_version       INTEGER NOT NULL,
    checksum_sha256      VARCHAR NOT NULL,
    http_status          INTEGER NOT NULL,
    quality_status       VARCHAR NOT NULL,
    quality_detail       VARCHAR,
    record_count         INTEGER NOT NULL,
    skipped_count        INTEGER NOT NULL,
    is_synthetic         BOOLEAN NOT NULL
);

CREATE TABLE raw.external_signals (
    record_key     VARCHAR PRIMARY KEY,
    source         VARCHAR NOT NULL,
    series_id      VARCHAR NOT NULL,
    entity_id      VARCHAR NOT NULL,
    metric         VARCHAR NOT NULL,
    unit           VARCHAR NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL,
    observed_date  DATE NOT NULL,
    value          DOUBLE NOT NULL,
    batch_id       VARCHAR NOT NULL,
    retrieved_at   TIMESTAMPTZ NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE raw.region_locations (
    region_id      VARCHAR PRIMARY KEY,
    name           VARCHAR NOT NULL,
    latitude       DOUBLE NOT NULL,
    longitude      DOUBLE NOT NULL,
    carbon_area    VARCHAR,
    eia_respondent VARCHAR
);

CREATE TABLE raw.sim_batches (
    batch_id  VARCHAR PRIMARY KEY,
    manifest  JSON NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);

-- Internal events. event_id is the idempotency key; event_date is the partition column.
CREATE TABLE raw.sim_events (
    event_id       VARCHAR PRIMARY KEY,
    event_type     VARCHAR NOT NULL,
    source         VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    entity_id      VARCHAR,
    occurred_at    TIMESTAMPTZ NOT NULL,
    published_at   TIMESTAMPTZ NOT NULL,
    event_date     DATE NOT NULL,
    trace_id       VARCHAR NOT NULL,
    correlation_id VARCHAR NOT NULL,
    causation_id   VARCHAR,
    is_synthetic   BOOLEAN NOT NULL,
    payload        JSON NOT NULL,
    batch_id       VARCHAR NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL
);
