"use client";

import { useId, useState, type KeyboardEvent, type MouseEvent } from "react";

import { day } from "../../lib/format";
import { DAY_MS, dayMs, extent, linear, niceTicks, runs } from "./scale";
import { useWidth } from "./useWidth";

export interface LinePoint {
  day: string; // ISO date
  value: number | null;
  lo?: number | null; // uncertainty band, when recorded
  hi?: number | null;
}

export interface Annotation {
  day: string;
  label: string;
}

export interface Reference {
  kind: "line" | "band" | "baseline"; // line = a limit; baseline = a comparison (quiet)
  y: number;
  y2?: number;
  label: string;
}

const PAD = { top: 16, right: 64, bottom: 24, left: 52 };

/**
 * One series over days, on one y-axis. Optional: an uncertainty band (lo..hi), a muted
 * comparison series, reference lines / bands (pre-registered thresholds) and vertical
 * annotations. Gaps in the data are gaps in the line. Hover and arrow keys show the same
 * values the table view lists.
 */
export function LineChart({
  points,
  comparison,
  comparisonLabel,
  references = [],
  annotations = [],
  format,
  title,
  height = 200,
  bandLabel,
  tickFormat,
  maxGapDays = 1.5,
}: {
  points: LinePoint[];
  comparison?: LinePoint[];
  comparisonLabel?: string;
  references?: Reference[];
  annotations?: Annotation[];
  format: (v: number) => string;
  title: string;
  height?: number;
  bandLabel?: string;
  tickFormat?: (v: number) => string; // y tick labels, when they need less than `format`
  maxGapDays?: number; // a longer gap between points breaks the line (daily data: 1.5)
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const tableId = useId();
  const pts = points.map((p) => ({ ...p, x: dayMs(p.day), y: p.value }));
  const cmp = (comparison ?? []).map((p) => ({ ...p, x: dayMs(p.day), y: p.value }));
  const ys = [
    ...pts.flatMap((p) => [p.value, p.lo, p.hi]),
    ...cmp.map((p) => p.value),
    ...references.flatMap((r) => [r.y, r.y2]),
  ].filter((v): v is number => typeof v === "number");
  const [y0, y1] = extent(ys);
  // A tick whose label repeats the previous one (integers on a half step) is dropped.
  const tick = tickFormat ?? format;
  // About one tick per 40 px of plot height (3 to 5).
  const target = Math.max(2, Math.min(4, Math.floor((height - PAD.top - PAD.bottom) / 40)));
  const ticks = niceTicks(y0, y1, target).filter((t, i, all) => i === 0 || tick(t) !== tick(all[i - 1] as number));
  const yLo = Math.min(y0, ticks[0] ?? y0);
  const yHi = Math.max(y1, ticks[ticks.length - 1] ?? y1);
  const xs = pts.map((p) => p.x);
  const [x0, x1] = extent(xs);
  const sx = linear(x0, x1, PAD.left, width - PAD.right);
  const sy = linear(yLo, yHi, height - PAD.bottom, PAD.top);

  const path = (run: { x: number; y: number | null }[]) =>
    run.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y as number).toFixed(1)}`).join("");
  const gap = maxGapDays * DAY_MS;
  const bandRuns = runs(
    pts.filter((p) => p.lo != null && p.hi != null).map((p) => ({ ...p, y: p.hi as number })),
    gap,
  );
  const last = [...pts].reverse().find((p) => p.value !== null);
  const xTicks = pickDays(pts.map((p) => p.day), Math.max(2, Math.floor(width / 110)));

  const nearest = (clientX: number, rect: DOMRect) => {
    const x = clientX - rect.left;
    let best = 0;
    let dist = Infinity;
    pts.forEach((p, i) => {
      const d = Math.abs(sx(p.x) - x);
      if (d < dist) {
        dist = d;
        best = i;
      }
    });
    return best;
  };
  const onMove = (e: MouseEvent<SVGRectElement>) =>
    setActive(nearest(e.clientX, e.currentTarget.ownerSVGElement!.getBoundingClientRect()));
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const step = e.key === "ArrowLeft" ? -1 : 1;
    setActive((a) => Math.min(pts.length - 1, Math.max(0, (a ?? pts.length - 1) + step)));
  };
  const a = active !== null ? pts[active] : undefined;

  return (
    <div className="chart-frame" ref={ref}>
      <svg
        className="chart"
        width={width}
        height={height}
        role="img"
        aria-label={title}
        aria-describedby={tableId}
        tabIndex={0}
        onKeyDown={onKey}
        onBlur={() => setActive(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={width - PAD.right} y1={sy(t)} y2={sy(t)} />
            <text className="tick" x={PAD.left - 8} y={sy(t)} dy="0.32em" textAnchor="end">
              {tick(t)}
            </text>
          </g>
        ))}
        {references.map((r) =>
          r.kind === "band" && r.y2 !== undefined ? (
            <g key={r.label}>
              <rect
                className="threshold-band"
                x={PAD.left}
                width={width - PAD.left - PAD.right}
                y={sy(Math.max(r.y, r.y2))}
                height={Math.abs(sy(r.y) - sy(r.y2))}
              />
              <text className="annotation-label" x={width - PAD.right + 6} y={sy(Math.max(r.y, r.y2))} dy="0.9em">
                {r.label}
              </text>
            </g>
          ) : (
            // Labelled inside the plot, top left of the line, clear of the end-value label.
            <g key={r.label}>
              <line className={r.kind === "baseline" ? "baseline-line" : "threshold"} x1={PAD.left} x2={width - PAD.right} y1={sy(r.y)} y2={sy(r.y)} />
              <text className="annotation-label" x={PAD.left + 4} y={sy(r.y) - 4}>
                {r.label}
              </text>
            </g>
          ),
        )}
        {bandRuns.map((run, i) => {
          const top = run.map((p) => `${sx(p.x).toFixed(1)},${sy(p.hi as number).toFixed(1)}`);
          const bottom = [...run].reverse().map((p) => `${sx(p.x).toFixed(1)},${sy(p.lo as number).toFixed(1)}`);
          return <polygon key={i} className="band" points={[...top, ...bottom].join(" ")} />;
        })}
        {runs(cmp, gap).map((run, i) => (
          <path key={`c${i}`} className="series-muted" d={path(run)} />
        ))}
        {runs(pts, gap).map((run, i) => (
          <path key={i} className="series" d={path(run)} />
        ))}
        {annotations.map((n) => (
          <g key={`${n.day}-${n.label}`}>
            <line className="annotation" x1={sx(dayMs(n.day))} x2={sx(dayMs(n.day))} y1={PAD.top} y2={height - PAD.bottom} />
            <text className="annotation-label" x={sx(dayMs(n.day)) + 4} y={PAD.top} dy="0.7em">
              {n.label}
            </text>
          </g>
        ))}
        {last ? (
          <g>
            <circle className="end-dot" cx={sx(last.x)} cy={sy(last.value as number)} r={4} />
            <text className="end-label" x={sx(last.x) + 8} y={sy(last.value as number)} dy="0.32em">
              {format(last.value as number)}
            </text>
          </g>
        ) : null}
        {xTicks.map((d) => (
          <text key={d} className="tick" x={sx(dayMs(d))} y={height - 6} textAnchor="middle">
            {day(d)}
          </text>
        ))}
        <line className="axis-line" x1={PAD.left} x2={width - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} />
        {a ? <line className="cursor" x1={sx(a.x)} x2={sx(a.x)} y1={PAD.top} y2={height - PAD.bottom} /> : null}
        <rect
          x={PAD.left}
          y={0}
          width={Math.max(0, width - PAD.left - PAD.right)}
          height={height}
          fill="transparent"
          onMouseMove={onMove}
          onMouseLeave={() => setActive(null)}
        />
      </svg>
      {a ? (
        <div className="chart-tip" style={{ left: Math.min(sx(a.x) + 10, width - 160) }} role="status">
          {day(a.day)}: {a.value === null ? "no value" : format(a.value)}
          {a.lo != null && a.hi != null ? ` (${format(a.lo)} to ${format(a.hi)})` : ""}
        </div>
      ) : null}
      <details className="table-view" id={tableId}>
        <summary>Table of these values</summary>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th className="r">Value</th>
                {bandLabel ? <th className="r">{bandLabel}</th> : null}
                {comparison ? <th className="r">{comparisonLabel ?? "Comparison"}</th> : null}
              </tr>
            </thead>
            <tbody>
              {pts.map((p, i) => (
                <tr key={p.day}>
                  <td>{p.day}</td>
                  <td className="r">{p.value === null ? "no value" : format(p.value)}</td>
                  {bandLabel ? (
                    <td className="r">
                      {p.lo != null && p.hi != null ? `${format(p.lo)} to ${format(p.hi)}` : "not recorded"}
                    </td>
                  ) : null}
                  {comparison ? (
                    <td className="r">{cmp[i]?.value != null ? format(cmp[i]!.value as number) : ""}</td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function pickDays(days: string[], n: number): string[] {
  if (days.length <= n) return days;
  const step = (days.length - 1) / (n - 1);
  return Array.from({ length: n }, (_, i) => days[Math.round(i * step)] as string);
}
