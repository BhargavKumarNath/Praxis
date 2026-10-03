# ADR 0002: Postgres control plane, BigQuery and GCS analytical plane

Status: Accepted (Phase 0)

## Context
Operational state is small, mutable and transactional. Event history and marts are large,
append-mostly and scanned analytically. One database cannot serve both well or cheaply.

## Decision
* Postgres (Supabase first, portable to Cloud SQL / AlloyDB) holds mutable operational
  state: customers, subscriptions, dunning state, decisions, dedupe keys, audit.
* BigQuery holds analytical facts and marts, partitioned by event date.
* GCS holds immutable or append-only raw data and model artifacts.
* Money uses integer minor units or fixed decimal, never binary floats. Time is
  timezone-aware UTC. All schema changes go through migrations.

## Consequences
* Bulk history never lands in Postgres, protecting connection and storage limits.
* Cross-plane consistency is eventual, driven by events.
* Repository layer must keep Postgres access portable.
