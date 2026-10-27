import type { ReactNode } from "react";

import type { Figure } from "../lib/types";
import { Sparkline } from "./charts/Sparkline";
import { Tag } from "./Tag";

export interface ReadoutItem {
  label: string;
  figure: Figure;
  format: (v: number) => string;
  unit?: string; // shown after the value; defaults to none (the format carries it)
  compare?: (value: number, baseline: number) => ReactNode;
  note?: ReactNode; // trust line: source, age, interval
  stale?: boolean;
  hero?: boolean;
}

/**
 * The head of a view: up to six figures separated by hairlines. A missing value is never a
 * blank or a zero: the cell says NO DATA and gives the server's reason.
 */
export function Readout({ items }: { items: ReadoutItem[] }) {
  return (
    <div className="readout" style={{ ["--cells" as string]: Math.min(items.length, 6) }}>
      {items.map((item) => (
        <ReadoutCell key={item.label} item={item} />
      ))}
    </div>
  );
}

function ReadoutCell({ item }: { item: ReadoutItem }) {
  const { figure } = item;
  const value = figure.value;
  return (
    <div className={`readout-cell${item.hero ? " hero" : ""}`} data-testid={`readout-${item.label}`}>
      <span className="readout-label">{item.label}</span>
      {value === null ? (
        <>
          <span className="readout-value" data-state="missing">
            <Tag tone="stale">No data</Tag>
          </span>
          <p className="readout-reason">{figure.reason}</p>
        </>
      ) : (
        <>
          <span className="readout-value" data-state={item.stale ? "stale" : "fresh"}>
            {item.format(value)}
            {item.unit ? <span className="unit">{item.unit}</span> : null}
          </span>
          {figure.baseline !== null && item.compare ? (
            <div className="readout-compare">
              {item.compare(value, figure.baseline)} {figure.baseline_label}
            </div>
          ) : null}
          {figure.series.length > 1 ? (
            <Sparkline points={figure.series.slice(-28)} label={`${item.label}, last 28 days`} />
          ) : null}
        </>
      )}
      {item.note ? <div className="readout-compare">{item.note}</div> : null}
    </div>
  );
}
