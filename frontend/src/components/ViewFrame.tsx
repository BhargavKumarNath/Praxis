"use client";

import type { ReactNode } from "react";

import { duration, utcTime } from "../lib/format";
import { REFRESH_MS, useNow, useResource } from "../lib/useResource";
import { ErrorState } from "./ErrorState";
import { StaleTag, SyntheticTag } from "./Tag";

// A view is stale when its last good read is older than two refresh intervals.
const STALE_AFTER_S = (2 * REFRESH_MS) / 1000;

/**
 * Head of a view plus its data life cycle. While a refresh runs the last good render stays on
 * screen (dimmed by the panels). If refreshes fail, the old numbers stay visible but the view
 * is marked STALE with their age, and the error (with correlation id) is shown above them.
 */
export function ViewFrame<T extends { generated_at: string }>({
  path,
  title,
  deck,
  what,
  tags,
  children,
}: {
  path: string;
  title: string;
  deck: ReactNode;
  what: string;
  tags?: ReactNode;
  children: (data: T, refreshing: boolean) => ReactNode;
}) {
  const res = useResource<T>(path);
  const now = useNow();
  const age = res.readAt ? (now.getTime() - res.readAt.getTime()) / 1000 : null;
  const stale = age !== null && (age > STALE_AFTER_S || res.error !== null);

  return (
    <>
      <header className="view-head">
        <div>
          <h1>{title}</h1>
          <p className="deck">{deck}</p>
        </div>
        <div className="view-meta">
          <div className="tags">
            {tags}
            <SyntheticTag />
            {stale && age !== null ? <StaleTag ageS={age} /> : null}
          </div>
          {res.data ? (
            <span data-testid="read-at">
              Read {utcTime(res.data.generated_at)}
              {age !== null ? `, ${duration(age)} ago` : ""}
            </span>
          ) : (
            <span>{res.loading ? "Reading…" : "Not read"}</span>
          )}
          <button type="button" className="quiet" onClick={res.reload} disabled={res.loading}>
            Read now
          </button>
        </div>
      </header>
      {res.error ? <ErrorState error={res.error} what={what} onRetry={res.reload} /> : null}
      {res.data ? children(res.data, res.loading) : null}
    </>
  );
}
