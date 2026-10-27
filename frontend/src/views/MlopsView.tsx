"use client";

import { LineChart, type Reference } from "../components/charts/LineChart";
import { PanelFrame } from "../components/PanelFrame";
import { Readout } from "../components/Readout";
import { StatusTag, Tag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { decimal, duration, percent, utc, words } from "../lib/format";
import type { MlopsBody, ModelVersion, MonitoringRow } from "../lib/types";

const STAGE_TONE: Record<string, string> = {
  champion: "ok",
  challenger: "pending",
  candidate: "acknowledged",
  shadow: "pending",
  canary: "pending",
  retired: "resolved",
  rejected: "rejected",
  rolled_back: "failed",
};

export function MlopsView() {
  return (
    <ViewFrame<MlopsBody>
      path="/mlops"
      title="ML operations"
      what="the ML operations view"
      deck="Which model versions serve, how each was validated and promoted, and what monitoring sees against its pre-registered limits. Retraining and promotion are separate steps; nothing is promoted automatically."
    >
      {(data, refreshing) => <MlopsPanels data={data} refreshing={refreshing} />}
    </ViewFrame>
  );
}

export function MlopsPanels({ data, refreshing }: { data: MlopsBody; refreshing: boolean }) {
  const p = data.panels;
  const s = p.serving.data;
  const band = p.monitoring.data?.thresholds.coverage_80_band;
  return (
    <>
      {s ? (
        <Readout
          items={[
            {
              label: "Calibration, 80% interval",
              figure: s.calibration_80,
              format: (v) => percent(v, 1),
              note: band ? `target ${percent(band[0], 0)} to ${percent(band[1], 0)}` : undefined,
            },
            { label: "Drift alarm", figure: s.drift_alarm, format: (v) => (v > 0 ? "firing" : "quiet") },
            { label: "Forecast age", figure: s.forecast_age_hours, format: (v) => `${decimal(v, 0)} h` },
            { label: "Oldest data source", figure: s.oldest_source_age_s, format: (v) => duration(v) },
          ]}
        />
      ) : null}
      <p className="notice">{data.note}</p>
      <div className="grid section">
        <PanelFrame panel={p.versions} title="Model versions" className="span-12" refreshing={refreshing}
          sub="Every registered version with the data, features and code it was built from">
          {(d) => <VersionsTable versions={d.versions} champions={d.champions} />}
        </PanelFrame>

        <PanelFrame panel={p.monitoring} title="Monitoring" className="span-12" refreshing={refreshing}
          sub="Drift, shadow and probation reports against the limits in configs/mlops">
          {(d) => <Monitoring reports={d.reports} thresholds={d.thresholds} />}
        </PanelFrame>

        <PanelFrame panel={p.events} title="Lifecycle" className="span-7" refreshing={refreshing}
          sub="Every stage change, who or what made it, and why">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>When</th><th>Version</th><th>Change</th><th>By</th><th>Reason</th></tr></thead>
                <tbody>
                  {d.events.map((e) => (
                    <tr key={`${e.decision_id}-${e.model_version}-${e.to_stage}`} data-testid={`event-${e.trigger}`}>
                      <td>{utc(e.occurred_at)}</td>
                      <td className="mono">{e.model_version}</td>
                      <td>
                        {e.from_stage ? `${words(e.from_stage)} to ` : ""}{words(e.to_stage)}
                        {e.trigger.endsWith(".forced") ? <> <Tag tone="degraded">Forced</Tag></> : null}
                      </td>
                      <td className="mono">{e.actor}</td>
                      <td className="secondary">{e.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>

        <PanelFrame panel={p.retraining} title="Retraining requests" className="span-5" refreshing={refreshing}
          sub="A request trains a candidate; it never replaces the champion">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>As of</th><th>Model</th><th>Triggers</th><th>Status</th><th>Candidate</th></tr></thead>
                <tbody>
                  {d.requests.map((r) => (
                    <tr key={r.request_id}>
                      <td>{r.as_of}</td>
                      <td>{words(r.model_name)}</td>
                      <td>{r.triggers.map(words).join(", ")}</td>
                      <td><StatusTag status={r.status} /></td>
                      <td className="mono">{r.candidate_version ?? "none yet"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>
      </div>
    </>
  );
}

function backtest(v: ModelVersion): { vwape: number | null; coverage80: number | null } {
  const bt = (v.metrics.backtest ?? {}) as Record<string, Record<string, number>>;
  const own = bt.hybrid ?? Object.values(bt)[0];
  return { vwape: own?.vwape ?? null, coverage80: own?.coverage_80 ?? null };
}

function VersionsTable({ versions, champions }: { versions: ModelVersion[]; champions: Record<string, string> }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Version</th><th>Stage</th><th>Validation</th><th className="r">Backtest vWAPE</th>
            <th className="r">80% coverage</th><th>Data</th><th>Features</th><th>Code</th><th>Checksum</th><th>Trained</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => {
            const m = backtest(v);
            const serving = champions[v.model_name] === v.model_version;
            return (
              <tr key={v.model_version} data-testid={`version-${v.model_version}`}>
                <td>
                  <span className="mono">{v.model_version}</span>
                  <div className="panel-sub">{words(v.model_name)}{serving ? ", serving" : ""}</div>
                </td>
                <td><StatusTag status={STAGE_TONE[v.stage] ?? "neutral"} label={words(v.stage)} /></td>
                <td>
                  {v.validation === null ? (
                    <span className="muted">not validated</span>
                  ) : v.validation.passed ? (
                    <StatusTag status="ok" label="Passed" />
                  ) : (
                    <><StatusTag status="failed" label="Failed" /><div className="panel-sub">{v.validation.failed_checks.map(words).join(", ")}</div></>
                  )}
                </td>
                <td className="r">{m.vwape === null ? <span className="muted">not recorded</span> : percent(m.vwape, 1)}</td>
                <td className="r">{m.coverage80 === null ? <span className="muted">not recorded</span> : percent(m.coverage80, 1)}</td>
                <td className="mono">{v.data_version}</td>
                <td className="mono">{v.feature_version}</td>
                <td className="mono">{v.code_revision}</td>
                <td className="mono" title={v.artifact_checksum}>{v.artifact_checksum.slice(0, 12)}</td>
                <td>{utc(v.trained_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface Metric {
  key: string;
  title: string;
  format: (v: number) => string;
  references: Reference[];
}

function Monitoring({ reports, thresholds }: { reports: MonitoringRow[]; thresholds: NonNullable<MlopsBody["panels"]["monitoring"]["data"]>["thresholds"] }) {
  const [lo, hi] = thresholds.coverage_80_band;
  const metrics: Metric[] = [
    { key: "coverage_80", title: "80% interval coverage", format: (v) => percent(v, 0), references: [{ kind: "band", y: lo, y2: hi, label: "target" }] },
    { key: "vwape_ratio", title: "vWAPE against the reference window", format: (v) => `${decimal(v, 2)}×`, references: [{ kind: "line", y: thresholds.max_vwape_ratio_vs_reference, label: "limit" }] },
    { key: "prediction_psi", title: "Prediction drift (PSI)", format: (v) => decimal(v, 2), references: [{ kind: "line", y: thresholds.psi_alert, label: "alert" }] },
  ];
  const byModel = new Map<string, MonitoringRow[]>();
  for (const r of reports) {
    const key = `${r.model_version}·${r.kind}`;
    byModel.set(key, [...(byModel.get(key) ?? []), r]);
  }
  const groups = [...byModel.entries()].sort();
  return (
    <>
      {groups.map(([key, rows]) => {
        const sorted = [...rows].sort((a, b) => a.as_of.localeCompare(b.as_of));
        const fired = sorted.filter((r) => r.fired);
        const first = sorted[0]!;
        return (
          <div key={key} className="monitor-group" data-testid={`monitor-${first.model_version}-${first.kind}`}>
            <h3 className="subhead">
              <span className="mono">{first.model_version}</span>, {first.kind} reports, {sorted.length} days
              {fired.length ? <> <Tag tone="degraded">{`Fired on ${fired.length}`}</Tag></> : null}
            </h3>
            <div className="multiples">
              {metrics
                .filter((m) => sorted.some((r) => r.series[m.key] != null))
                .map((m) => (
                  <figure key={m.key} className="multiple">
                    <figcaption>{m.title}</figcaption>
                    <LineChart
                      title={`${first.model_version} ${m.title}`}
                      height={140}
                      format={m.format}
                      references={m.references}
                      annotations={fired.slice(0, 1).map((r) => ({ day: r.as_of, label: "fired" }))}
                      points={sorted.map((r) => ({ day: r.as_of, value: r.series[m.key] ?? null }))}
                    />
                  </figure>
                ))}
            </div>
          </div>
        );
      })}
      <p className="chart-caption">Limits from {thresholds.source}.</p>
    </>
  );
}
