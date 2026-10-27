import type { ReactNode } from "react";

import type { Panel } from "../lib/types";
import { StatusTag, SyntheticTag, Tag } from "./Tag";

/**
 * One panel of a view. When the store had nothing (no_data) or did not answer (unavailable)
 * the panel says so with the server's reason, never a zero. The source line is always shown.
 */
export function PanelFrame<T>({
  panel,
  title,
  sub,
  className = "span-12",
  refreshing = false,
  extra,
  children,
}: {
  panel: Panel<T>;
  title: string;
  sub?: ReactNode;
  className?: string;
  refreshing?: boolean;
  extra?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  return (
    <section
      className={`panel ${className}`}
      data-refreshing={refreshing}
      data-status={panel.status}
      aria-label={title}
    >
      <header className="panel-head">
        <div>
          <h2>{title}</h2>
          {sub ? <p className="panel-sub">{sub}</p> : null}
        </div>
        <div className="view-meta">
          <div className="tags">
            {extra}
            {panel.synthetic ? <SyntheticTag /> : <Tag tone="neutral">{provenance(panel.source)}</Tag>}
            {panel.status !== "ok" ? <StatusTag status={panel.status} label={label(panel)} /> : null}
          </div>
          <span className="panel-source">From {panel.source}</span>
        </div>
      </header>
      {panel.status === "ok" && panel.data !== null ? (
        children(panel.data)
      ) : (
        <div className="empty" data-testid="panel-empty">
          <p>{panel.reason}</p>
        </div>
      )}
    </section>
  );
}

// Data that is not simulator output is either a repository config file or an external source.
function provenance(source: string): string {
  return source.startsWith("config") ? "Config" : "External";
}

function label(panel: Panel<unknown>): string {
  return panel.status === "no_data" ? "No data" : "Unavailable";
}
