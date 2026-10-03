"""Failure taxonomy for ingestion. Each class maps to a distinct, observable outcome."""

from __future__ import annotations


class IngestError(Exception):
    """Base class for ingestion failures."""


class SourceUnavailableError(IngestError):
    """Timeout, connection failure, rate limit or 5xx persisting after bounded retries."""


class SourceRejectedError(IngestError):
    """Non-retryable 4xx: the request itself is wrong (bad parameter, bad key)."""


class SchemaDriftError(IngestError):
    """The response no longer matches the contract. The raw batch is quarantined."""


class MissingCredentialError(IngestError):
    """A source needs an API key that is not configured."""


class RawIntegrityError(IngestError):
    """A stored raw body does not match its recorded checksum."""
