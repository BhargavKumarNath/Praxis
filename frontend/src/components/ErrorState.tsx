import { describeError } from "../lib/api";
import type { ApiError } from "../lib/types";

/** A failed API call: what failed, the correlation id to quote, and a retry. Never a trace. */
export function ErrorState({
  error,
  what,
  onRetry,
}: {
  error: ApiError;
  what: string;
  onRetry?: () => void;
}) {
  return (
    <section className="error-state" role="alert" aria-live="assertive">
      <h2>Could not read {what}</h2>
      <p>{describeError(error)}</p>
      <p className="muted">
        Correlation id{" "}
        <span className="mono" data-testid="correlation-id">
          {error.correlationId ?? "not returned"}
        </span>
        {error.status ? <>, HTTP {error.status}</> : null}. Quote it when you look up the API logs.
      </p>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Read again
        </button>
      ) : null}
    </section>
  );
}
