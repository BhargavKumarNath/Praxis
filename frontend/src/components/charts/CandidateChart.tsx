"use client";

import { useId, useState } from "react";

import type { Candidate } from "../../lib/types";
import { extent, linear, niceTicks } from "./scale";
import { useWidth } from "./useWidth";

const PAD = { top: 14, right: 16, bottom: 34, left: 64 };

/**
 * The optimiser's view of one decision: the change in objective (mean, with its p05..p95
 * band) at every candidate price. Feasible candidates are filled, candidates a constraint
 * ruled out are hollow; the current and chosen prices are marked. One y-axis.
 */
export function CandidateChart({
  candidates,
  current,
  chosen,
  formatPrice,
  formatValue,
  height = 220,
}: {
  candidates: Candidate[];
  current: number | null;
  chosen: number | null;
  formatPrice: (micros: number) => string;
  formatValue: (v: number) => string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const tableId = useId();
  const pts = [...candidates].sort((a, b) => a.price_micros - b.price_micros);
  const [x0, x1] = extent(pts.map((c) => c.price_micros));
  const [y0, y1] = extent(pts.flatMap((c) => [c.delta_p05, c.delta_p95, 0]));
  const ticks = niceTicks(y0, y1);
  const sx = linear(x0, x1, PAD.left, width - PAD.right);
  const sy = linear(Math.min(y0, ticks[0] ?? y0), Math.max(y1, ticks.at(-1) ?? y1), height - PAD.bottom, PAD.top);
  const xTicks = niceTicks(x0, x1, Math.max(2, Math.floor(width / 140))).filter((t) => t >= x0 && t <= x1);
  const band = [
    ...pts.map((c) => `${sx(c.price_micros).toFixed(1)},${sy(c.delta_p95).toFixed(1)}`),
    ...[...pts].reverse().map((c) => `${sx(c.price_micros).toFixed(1)},${sy(c.delta_p05).toFixed(1)}`),
  ].join(" ");
  const line = pts.map((c, i) => `${i ? "L" : "M"}${sx(c.price_micros).toFixed(1)},${sy(c.delta_mean).toFixed(1)}`).join("");
  const a = active !== null ? pts[active] : undefined;

  const marker = (price: number | null, label: string, dy: number) =>
    price === null || price < x0 || price > x1 ? null : (
      <g>
        <line className="annotation" x1={sx(price)} x2={sx(price)} y1={PAD.top} y2={height - PAD.bottom} />
        <text className="annotation-label" x={sx(price) + 4} y={PAD.top + dy} dy="0.7em">
          {label}
        </text>
      </g>
    );

  return (
    <div className="chart-frame" ref={ref}>
      <ul className="legend" aria-hidden="true">
        <li><span className="key-line" /> mean change in objective</li>
        <li><span className="key-band" /> p05 to p95</li>
        <li><svg width="10" height="10"><circle cx="5" cy="5" r="3.5" className="point" style={{ fill: "var(--accent)" }} /></svg> feasible</li>
        <li><svg width="10" height="10"><circle cx="5" cy="5" r="3.5" style={{ fill: "none", stroke: "var(--ink-3)", strokeWidth: 1.5 }} /></svg> ruled out by a constraint</li>
      </ul>
      <svg
        className="chart"
        width={width}
        height={height}
        role="img"
        aria-label="Change in objective by candidate price"
        aria-describedby={tableId}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
          e.preventDefault();
          const step = e.key === "ArrowLeft" ? -1 : 1;
          setActive((i) => Math.min(pts.length - 1, Math.max(0, (i ?? 0) + step)));
        }}
        onBlur={() => setActive(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line className={t === 0 ? "axis-line" : "grid-line"} x1={PAD.left} x2={width - PAD.right} y1={sy(t)} y2={sy(t)} />
            <text className="tick" x={PAD.left - 8} y={sy(t)} dy="0.32em" textAnchor="end">
              {formatValue(t)}
            </text>
          </g>
        ))}
        <polygon className="band" points={band} />
        <path className="series" d={line} />
        {marker(current, "current", 0)}
        {chosen !== current ? marker(chosen, "chosen", 14) : null}
        {pts.map((c, i) => (
          <circle
            key={c.price_micros}
            className={c.feasible ? "point" : "point-infeasible"}
            cx={sx(c.price_micros)}
            cy={sy(c.delta_mean)}
            r={active === i ? 5 : 3.5}
            onMouseEnter={() => setActive(i)}
            onMouseLeave={() => setActive(null)}
          />
        ))}
        {xTicks.map((t) => (
          <text key={t} className="tick" x={sx(t)} y={height - PAD.bottom + 16} textAnchor="middle">
            {formatPrice(t)}
          </text>
        ))}
        <text className="tick" x={width - PAD.right} y={height - 4} textAnchor="end">
          candidate price per unit
        </text>
      </svg>
      {a ? (
        <div className="chart-tip" style={{ left: Math.min(sx(a.price_micros) + 10, width - 260) }} role="status">
          {formatPrice(a.price_micros)}: {formatValue(a.delta_mean)} ({formatValue(a.delta_p05)} to {formatValue(a.delta_p95)})
          {a.feasible ? "" : `, ruled out: ${a.violations.join(", ")}`}
        </div>
      ) : null}
      <details className="table-view" id={tableId}>
        <summary>Table of all {pts.length} candidates</summary>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="r">Price</th>
                <th className="r">Mean change</th>
                <th className="r">p05 to p95</th>
                <th className="r">P(improvement)</th>
                <th>Ruled out by</th>
              </tr>
            </thead>
            <tbody>
              {pts.map((c) => (
                <tr key={c.price_micros}>
                  <td className="r">{formatPrice(c.price_micros)}</td>
                  <td className="r">{formatValue(c.delta_mean)}</td>
                  <td className="r">{formatValue(c.delta_p05)} to {formatValue(c.delta_p95)}</td>
                  <td className="r">{(c.prob_improvement * 100).toFixed(1)}%</td>
                  <td>{c.feasible ? "feasible" : c.violations.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
