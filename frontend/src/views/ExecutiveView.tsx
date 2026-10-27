"use client";

import { LineChart } from "../components/charts/LineChart";
import { PanelFrame } from "../components/PanelFrame";
import { Readout, type ReadoutItem } from "../components/Readout";
import { Tag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { compact, day, decimal, integer, moneyCompact, percent, signed } from "../lib/format";
import type { ExecutiveBody, ExecutiveFigure, ExecutiveKey, Panel } from "../lib/types";

interface Spec {
  key: ExecutiveKey;
  label: string;
  format: (v: number) => string;
  tick?: (v: number) => string;
  // How the latest value compares with its baseline, in the unit a reader thinks in.
  compare: (v: number, base: number) => string;
}

const relative = (v: number, base: number) =>
  base === 0 ? "no baseline" : `${signed(((v - base) / Math.abs(base)) * 100, 0)}% against`;
const points = (v: number, base: number) => `${signed((v - base) * 100, 1)} pts against`;
const count = (v: number, base: number) => `${signed(v - base, 0)} against`;

const SPECS: Spec[] = [
  { key: "revenue", label: "Revenue, per day", format: (v) => moneyCompact(v, "GBP"), tick: (v) => moneyCompact(v, "GBP").replace(".0K", "K"), compare: relative },
  { key: "gross_margin", label: "Gross margin, usage", format: (v) => percent(v, 1), compare: points },
  { key: "active_customers", label: "Active customers", format: integer, compare: count },
  { key: "churn", label: "Churned, per day", format: integer, compare: count },
  { key: "demand", label: "Served usage, per day", format: (v) => `${compact(v)} units`, tick: compact, compare: relative },
  { key: "recovery_rate", label: "Failed-first-attempt invoices paid", format: (v) => percent(v, 0), compare: points },
  { key: "price_index", label: "List price index", format: (v) => decimal(v, 1), compare: count },
];

export function ExecutiveView() {
  return (
    <ViewFrame<ExecutiveBody>
      path="/executive"
      title="Executive"
      what="the executive view"
      deck="Revenue, margin, customers and recovery from the warehouse marts, each against the median of its previous 28 days. The business is simulated: these figures describe the simulator's world, not a real company."
    >
      {(data, refreshing) => <ExecutivePanels data={data} refreshing={refreshing} />}
    </ViewFrame>
  );
}

function latestDays(panels: Record<ExecutiveKey, Panel<ExecutiveFigure>>): string | null {
  const days = Object.values(panels).flatMap((p) => (p.data && p.data.figure.value !== null ? [p.data.latest_day] : []));
  return days.length ? days.sort().at(-1)! : null;
}

function behind(latest: string, newest: string | null): number {
  if (!newest) return 0;
  return Math.round((Date.parse(newest) - Date.parse(latest)) / 86_400_000);
}

export function ExecutivePanels({ data, refreshing }: { data: ExecutiveBody; refreshing: boolean }) {
  const p = data.panels;
  const newest = latestDays(p);
  const head = SPECS.slice(0, 4).map((s): ReadoutItem | null => {
    const panel = p[s.key];
    if (!panel.data) return null;
    const lag = behind(panel.data.latest_day, newest);
    return {
      label: s.label,
      figure: panel.data.figure,
      format: s.format,
      compare: s.compare,
      hero: s.key === "revenue",
      stale: lag > 0,
      note: <AsOf latest={panel.data.latest_day} lag={lag} />,
    };
  });
  return (
    <>
      {head.every((h) => h !== null) ? <Readout items={head as ReadoutItem[]} /> : null}
      <p className="notice">
        {data.note} Window: the last {data.window_days} days in the warehouse{newest ? `, to ${day(newest)}` : ""}.
      </p>
      <div className="grid section">
        {SPECS.map((s) => (
          <PanelFrame key={s.key} panel={p[s.key]} title={s.label} className="span-6" refreshing={refreshing}>
            {(d) => <MetricChart spec={s} d={d} lag={behind(d.latest_day, newest)} />}
          </PanelFrame>
        ))}
      </div>
    </>
  );
}

function AsOf({ latest, lag }: { latest: string; lag: number }) {
  return lag > 0 ? (
    <span data-testid="lagging">
      <Tag tone="stale">{`${lag} days behind`}</Tag> as of {day(latest)}
    </span>
  ) : (
    <>as of {day(latest)}</>
  );
}

function MetricChart({ spec, d, lag }: { spec: Spec; d: ExecutiveFigure; lag: number }) {
  const f = d.figure;
  return (
    <>
      <p className="readout-value" style={{ marginTop: 0 }} data-testid={`exec-${spec.key}`}>
        {f.value === null ? <Tag tone="stale">No data</Tag> : spec.format(f.value)}
        <span className="unit"><AsOf latest={d.latest_day} lag={lag} /></span>
      </p>
      {f.value === null ? <p className="readout-reason">{f.reason}</p> : null}
      {f.series.length > 1 ? (
        <LineChart
          title={`${spec.label}, daily`}
          format={spec.format}
          tickFormat={spec.tick}
          height={160}
          points={f.series.map((x) => ({ day: x.day, value: x.value }))}
          references={f.baseline !== null ? [{ kind: "baseline", y: f.baseline, label: "28-day median" }] : []}
        />
      ) : null}
      <p className="chart-caption">{d.definition}</p>
    </>
  );
}
