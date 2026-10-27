import { dayMs, extent, linear, runs } from "./scale";

/** A 28-point trend beside a readout value: no axes; the last point is the dot. */
export function Sparkline({
  points,
  width = 120,
  height = 28,
  label,
}: {
  points: { day: string; value: number | null }[];
  width?: number;
  height?: number;
  label: string;
}) {
  const pts = points.map((p) => ({ x: dayMs(p.day), y: p.value }));
  const values = pts.map((p) => p.y).filter((v): v is number => v !== null);
  if (values.length < 2) return null;
  const [y0, y1] = extent(values);
  const [x0, x1] = extent(pts.map((p) => p.x));
  const sx = linear(x0, x1, 2, width - 4);
  const sy = linear(y0, y1, height - 3, 3);
  const last = [...pts].reverse().find((p) => p.y !== null);
  return (
    <svg className="chart spark" width={width} height={height} role="img" aria-label={label}>
      {runs(pts).map((run, i) => (
        <path
          key={i}
          className="series"
          style={{ strokeWidth: 1.5 }}
          d={run.map((p, j) => `${j ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y as number).toFixed(1)}`).join("")}
        />
      ))}
      {last ? <circle className="end-dot" cx={sx(last.x)} cy={sy(last.y as number)} r={2.5} /> : null}
    </svg>
  );
}
