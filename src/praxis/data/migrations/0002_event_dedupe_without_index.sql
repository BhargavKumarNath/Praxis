-- ADR 0028: raw.sim_events stays unique on event_id WITHOUT an in-memory primary-key index.
--
-- DuckDB keeps a PRIMARY KEY as an ART index in memory (measured ~180 MB per million rows;
-- Phase 14 ran out of memory between 4.9M and 6.1M rows per file). Uniqueness is now kept by
-- the single writer (Warehouse) with an anti-join scoped to the event dates being written,
-- which zone maps prune to those days and which DuckDB can spill to disk.
--
-- Streaming writes land first in raw.sim_events_landing (append only, no constraint) and are
-- compacted into raw.sim_events (first landed occurrence of an id wins, ids already present
-- are skipped). Readers only ever read raw.sim_events.

CREATE TABLE raw.sim_events_next (
    event_id       VARCHAR NOT NULL,
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

INSERT INTO raw.sim_events_next SELECT * FROM raw.sim_events ORDER BY event_date, occurred_at;
DROP TABLE raw.sim_events;
ALTER TABLE raw.sim_events_next RENAME TO sim_events;

CREATE SEQUENCE raw.sim_events_landing_seq;

CREATE TABLE raw.sim_events_landing (
    seq            BIGINT NOT NULL DEFAULT nextval('raw.sim_events_landing_seq'),
    event_id       VARCHAR NOT NULL,
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
