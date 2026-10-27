"use client";

import { useId, useState } from "react";

import { day } from "../../lib/format";
import { linear, niceTicks } from "./scale";
import { useWidth } from "./useWidth";

const PAD = { top: 10, right: 8, bottom: 24, left: 36 };

/** Counts per day as paired bars (muted = the base count, accent = the outcome), one axis
 * from zero. For discrete daily counts, where a line would invent values between days. */
export function DayBars({
  days,
  base,
  outcome,
  title,
  height = 180,
}: {
  days: { day: string; base: number; outcome: number }[];
  base: string;
  outcome: string;
  title: string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const tableId = useId();
  const max = Math.max(1, ...days.flatMap((d) => [d.base, d.outcome]));
  const ticks = [...new Set(niceTicks(0, max).map((t) => Math.round(t)))].filter((t) => t >= 0);
  const top = Math.max(max, ticks.at(-1) ?? max);
  const sy = linear(0, top, height - PAD.bottom, PAD.top);
  const slot = (width - PAD.left - PAD.right) / Math.max(1, days.length);
  const bar = Math.max(1, Math.min(10, (slot - 4) / 2));
  const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(width / 70))));
  const a = active !== null ? days[active] : undefined;
  return (
    <div className="chart-frame" ref={ref}>
      <ul className="legend" aria-hidden="true">
        <li><span className="key-band" style={{ background: "var(--ink-3)", opacity: 0.55 }} /> {base}</li>
        <li><span className="key-band" style={{ background: "var(--accent)" }} /> {outcome}</li>
      </ul>
      <svg className="chart" width={width} height={height} role="img" aria-label={title} aria-describedby={tableId}>
        {ticks.map((t) => (
          <g key={t}>
            <line className={t === 0 ? "axis-line" : "grid-line"} x1={PAD.left} x2={width - PAD.right} y1={sy(t)} y2={sy(t)} />
            <text className="tick" x={PAD.left - 6} y={sy(t)} dy="0.32em" textAnchor="end">{t}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = PAD.left + i * slot + (slot - 2 * bar - 2) / 2;
          return (
            <g key={d.day} onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)}>
              <rect x={PAD.left + i * slot} y={PAD.top} width={slot} height={height - PAD.top - PAD.bottom} fill="transparent" />
              <rect className="bar-muted" x={x} y={sy(d.base)} width={bar} height={sy(0) - sy(d.base)} />
              <rect className="bar" x={x + bar + 2} y={sy(d.outcome)} width={bar} height={sy(0) - sy(d.outcome)} />
              {i % every === 0 ? (
                <text className="tick" x={PAD.left + i * slot + slot / 2} y={height - 6} textAnchor="middle">{day(d.day)}</text>
              ) : null}
            </g>
          );
        })}
      </svg>
      {a ? (
        <div className="chart-tip" style={{ left: Math.min(PAD.left + (active ?? 0) * slot + slot, width - 180) }} role="status">
          {day(a.day)}: {a.base} {base}, {a.outcome} {outcome}
        </div>
      ) : null}
      <details className="table-view" id={tableId}>
        <summary>Table of these values</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Day</th><th className="r">{base}</th><th className="r">{outcome}</th></tr></thead>
            <tbody>
              {days.map((d) => (
                <tr key={d.day}><td>{d.day}</td><td className="r">{d.base}</td><td className="r">{d.outcome}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
