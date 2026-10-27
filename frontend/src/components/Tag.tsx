import type { ReactNode } from "react";

import { duration } from "../lib/format";

export type Tone = "ok" | "degraded" | "stale" | "synthetic" | "neutral" | "accent";

export function Tag({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`tag tag-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function SyntheticTag() {
  return (
    <Tag tone="synthetic" title="Simulator output, not real business results">
      Synthetic
    </Tag>
  );
}

export function ShadowTag() {
  return (
    <Tag tone="neutral" title="Shadow evaluation: not validated for execution">
      Shadow
    </Tag>
  );
}

/** "STALE 3h 12m": an input older than its limit. The age is the number to read. */
export function StaleTag({ ageS, limit }: { ageS: number; limit?: string }) {
  return (
    <Tag tone="stale" title={limit ? `Older than its limit (${limit})` : "Not refreshed in time"}>
      Stale {duration(ageS)}
    </Tag>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  ok: "ok",
  fresh: "ok",
  completed: "ok",
  resolved: "neutral",
  degraded: "degraded",
  failed: "degraded",
  breached: "degraded",
  open: "degraded",
  acknowledged: "neutral",
  pending: "accent",
  executing: "accent",
  rejected: "neutral",
  stale: "stale",
  missing: "stale",
  no_data: "stale",
  unavailable: "degraded",
};

export function StatusTag({ status, label }: { status: string; label?: string }) {
  return <Tag tone={STATUS_TONE[status] ?? "neutral"}>{label ?? status.replace(/_/g, " ")}</Tag>;
}
