# ADR 0009: Order-independent, idempotent event processing

Status: Accepted (Phase 3)

## Context
Pub/Sub delivers at least once, possibly late, duplicated or out of order (ADR 0001). A
consumer can die after committing a side effect and before acking. Databases have brief
outages. Some messages are malformed or can never be processed. The control plane holds
money (the payment ledger) and lifecycle state, and must end in the right state whatever
the delivery pattern.

## Decision
* **Transport abstraction.** Consumers see a `Delivery` and return ACK or NACK. Two
  transports implement Pub/Sub semantics: Google Pub/Sub (emulator locally, Terraform in
  GCP) and a deterministic `MemoryBroker` with leases, backoff, delivery-attempt counting,
  DLQ forwarding and seeded fault injection (duplicates, delay, reorder). One `Topology`
  definition drives both, and a static test keeps Terraform in sync with it.
* **Routing.** One events topic. Subscriptions: `operational` (filter
  `attributes.stateful = "true"`, so bulk usage never reaches Postgres), `warehouse`,
  `monitoring`, and `dlq-inspect` on the DLQ topic. Producers set the `stateful`
  attribute from the event contract. Pub/Sub filters are immutable and at most 256 bytes,
  so we filter on one attribute rather than listing event types.
* **Idempotency store plus event-sourced aggregates.** The operational consumer runs one
  Postgres transaction per event. It inserts `(consumer, event_id)` into
  `processed_events`; if the row exists, the event is a no-op. Otherwise it takes an
  advisory lock on the aggregate, appends the event to `entity_events` and **refolds** the
  aggregate from its full log in canonical order (`occurred_at`, lifecycle rank,
  `event_id`). The projection is a pure function of the event *set*, so final state does
  not depend on arrival order or duplicates. An event whose predecessor is missing is
  *pending* and applies automatically once the predecessor arrives.
* **Explicit state machines everywhere.** Customer, subscription and invoice states change
  only through `TransitionTable`s. Postgres enforces the same rules again: CHECK
  constraints on state values, plus a trigger that allows only the moves listed in
  `allowed_state_moves` (the reachability closure of the Python tables; a test keeps the
  two equal). Money: `payment_ledger` has one row per invoice, BIGINT minor units, and
  `CHECK (state = 'paid') = (amount_paid_minor = amount_minor)`.
* **Failure policy** (`praxis.streaming.runtime`):
  * Decode failure (malformed JSON, unsupported schema version, unknown type, invalid
    payload): publish to the DLQ immediately, then ack. Redelivering the same bytes can
    never succeed.
  * `TransientError` (connection loss, timeout, deadlock): NACK, redeliver with
    exponential backoff.
  * Any other exception (poison): NACK. Pub/Sub's dead-letter policy forwards the message
    after `max_delivery_attempts` (5), so retries are bounded with no in-process retry
    loop.
* **Acks.** Non-batch workers ack each message as soon as it completes, like the
  streaming-pull client. Batch workers (the warehouse) ack a batch together and fall back
  to one-by-one processing on a non-transient failure, so one poison message cannot drag
  a whole batch into the DLQ.
* **Dead letters are visible and recoverable.** The DLQ inspector stores every dead
  letter in Postgres (reason, source subscription, attempts, trace and correlation IDs,
  original bytes up to 64 KiB). It is idempotent per (subscription, event). Redrive is an
  explicit operator action. It republishes decodable dead letters with the same
  `event_id`, so it is safe for subscriptions that had already processed them.
* **Replay.** The producer validates the whole batch, then writes it to an append-only,
  content-addressed archive (`source=/date=/hour=` layout, the same as GCS), then
  publishes. `replay_archive` republishes with the same IDs. Replaying into a live system
  changes nothing; replaying into an empty one rebuilds an identical control plane.
* **Tracing.** The runtime binds each event's `trace_id` and `correlation_id` before any
  handler runs. The IDs are recorded in `processed_events`, `entity_events`, the warehouse
  rows and `dead_letters`, and are carried as attributes so even unparseable messages stay
  traceable.

## Consequences
* Duplicate, delayed, reordered and crash-interrupted delivery give the same final state
  as an exactly-once in-order run (verified against the Phase 1 validator as an
  independent oracle).
* Every stateful event costs a refold of its aggregate. Lifecycle aggregates are small
  (fewer than 15 events), so this is cheap. Usage events are never folded.
* `processed_events` grows without bound. A retention job must keep it longer than the
  maximum redelivery and replay window (TODO before production scale). `entity_events`
  already deduplicates permanently.
* Measured finding: a consumer that crashes repeatedly also burns delivery attempts of the
  messages leased with it, so a crash storm can dead-letter innocent messages (batch
  consumers are worst affected). Correctness holds and redrive restores state exactly,
  but crash loops must page someone (Phase 11 alerting).
* Monitoring counters count deliveries, including duplicates. Its LRU-based duplicate
  detection is best-effort and resets on restart. Business counts come from the
  idempotent stores.
* The worker uses synchronous SQLAlchemy and psycopg 3 (not asyncpg): the Pub/Sub client
  is thread-based, and a sync worker is simpler to reason about. The repository layer
  stays portable to async later.
