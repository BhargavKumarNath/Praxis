"use client";

import { PanelFrame } from "../components/PanelFrame";
import { Readout } from "../components/Readout";
import { StaleTag, StatusTag, Tag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { decimal, duration, integer, percent, utc, words } from "../lib/format";
import type { Health, Inputs, ProcessInput, SourceHealth, SystemBody } from "../lib/types";

const ms = (v: number) => `${integer(v)} ms`;

export function SystemView() {
  return (
    <ViewFrame<SystemBody>
      path="/system"
      title="System"
      what="the system view"
      deck="Event lag, API latency and errors, queues and dead letters, the age of every external source, and what this costs. Fleet metrics are the latest values each process published; they have no history."
    >
      {(data, refreshing) => <SystemPanels data={data} refreshing={refreshing} />}
    </ViewFrame>
  );
}

export function SystemPanels({ data, refreshing }: { data: SystemBody; refreshing: boolean }) {
  const p = data.panels;
  const s = p.signals.data;
  return (
    <>
      {s ? (
        <Readout
          items={[
            { label: "Event lag p95, publish to ack", figure: s.event_lag_p95_ms, format: ms },
            { label: "API latency p95", figure: s.api_latency_p95_ms, format: ms },
            { label: "API 5xx rate", figure: s.api_error_rate, format: (v) => percent(v, 2) },
            { label: "Queue backlog, max", figure: s.queue_backlog_max, format: integer, unit: "messages" },
            { label: "Dead letters open, max", figure: s.dlq_backlog_max, format: integer, unit: "messages" },
          ]}
        />
      ) : null}
      <p className="notice">
        Telemetry scope: {data.inputs.telemetry_scope}. Upper bounds of histogram buckets, so
        quantiles are conservative. {data.note}
      </p>
      <div className="grid section">
        <InputsPanel inputs={data.inputs} />
        <PanelFrame panel={p.health} title="Alerts and SLOs" className="span-7" refreshing={refreshing}
          sub="Evaluated now against the pre-registered rules in configs/ops">
          {(h) => <HealthBody health={h} />}
        </PanelFrame>
        <PanelFrame panel={p.sources} title="External sources" className="span-12" refreshing={refreshing}
          sub="Age of the newest observation against each source's limit">
          {(d) => <SourcesTable sources={d.sources} />}
        </PanelFrame>
        <PanelFrame panel={p.dead_letters} title="Dead letters" className="span-6" refreshing={refreshing}>
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Subscription</th><th className="r">Open</th><th className="r">Redriven</th><th>Oldest open</th><th>Reasons</th></tr>
                </thead>
                <tbody>
                  {d.subscriptions.map((row) => (
                    <tr key={row.subscription}>
                      <td className="mono">{row.subscription}</td>
                      <td className="r">{integer(row.open)}</td>
                      <td className="r">{integer(row.redriven)}</td>
                      <td>{row.oldest_open_at ? utc(row.oldest_open_at) : "none open"}</td>
                      <td>{row.reasons.map(words).join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>
        <PanelFrame panel={p.cost} title="Cloud cost" className="span-6" refreshing={refreshing}
          extra={<Tag tone="neutral">Estimate</Tag>}>
          {(c) => (
            <>
              <p className="readout-value num" style={{ marginTop: 0 }}>
                £{decimal(c.monthly_estimate_gbp, 2)}<span className="unit">per month, estimated {c.as_of}</span>
              </p>
              <p className="panel-sub">{c.label}. {c.budget_note}</p>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>State</th><th className="r">£ / month</th></tr></thead>
                  <tbody>
                    {c.items.map((i) => (
                      <tr key={i.resource}>
                        <td>{i.resource}</td>
                        <td className="secondary">{i.state}</td>
                        <td className="r" style={{ whiteSpace: "nowrap" }}>{i.monthly_gbp === null ? <span title={i.basis}>not estimated</span> : decimal(i.monthly_gbp, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="chart-caption">Sources: {c.sources.join("; ")}.</p>
            </>
          )}
        </PanelFrame>
      </div>
    </>
  );
}

function InputsPanel({ inputs }: { inputs: Inputs }) {
  const processes = inputs.processes ?? [];
  const collectors = Object.entries(inputs.collectors ?? {});
  return (
    <section className="panel span-5" aria-label="Where these numbers came from">
      <header className="panel-head">
        <div>
          <h2>Where these numbers came from</h2>
          <p className="panel-sub">Processes that published a snapshot, and the pull collectors</p>
        </div>
      </header>
      {processes.length === 0 ? (
        <div className="empty">
          <p>
            No process has published a metric snapshot. Start the API, the consumers or the ops
            monitor (`python -m praxis.ops run`).
          </p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Process</th><th className="r">Instances</th><th className="r">Newest snapshot</th><th>State</th></tr></thead>
            <tbody>
              {groupProcesses(processes).map((g) => (
                <tr key={g.component} data-testid={`process-${g.component}`}>
                  <td>{g.component}</td>
                  <td className="r">{g.instances}</td>
                  <td className="r">{duration(g.newest.age_s)} ago</td>
                  <td>
                    {g.stale === g.instances ? (
                      <StaleTag ageS={g.newest.age_s} limit={`expired ${utc(g.newest.expires_at)}`} />
                    ) : (
                      <StatusTag status="ok" label={g.stale ? `Live, ${g.stale} stale` : "Live"} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <dl className="facts" style={{ marginTop: 12 }}>
        {collectors.map(([name, c]) => (
          <div key={name} style={{ display: "contents" }}>
            <dt>{words(name)} collector</dt>
            <dd>
              <StatusTag status={c.status === "ok" ? "ok" : "unavailable"} label={c.status} />
              {c.error ? <span className="muted"> {c.error}</span> : null}
            </dd>
          </div>
        ))}
      </dl>
      {inputs.stale_processes && inputs.stale_processes.length ? (
        <p className="notice">Stale processes are left out of every number above: {inputs.stale_processes.join(", ")}.</p>
      ) : null}
    </section>
  );
}

function groupProcesses(processes: ProcessInput[]) {
  const groups = new Map<string, { component: string; instances: number; stale: number; newest: ProcessInput }>();
  for (const proc of processes) {
    const g = groups.get(proc.component);
    if (!g) {
      groups.set(proc.component, { component: proc.component, instances: 1, stale: proc.stale ? 1 : 0, newest: proc });
    } else {
      g.instances += 1;
      g.stale += proc.stale ? 1 : 0;
      if (proc.age_s < g.newest.age_s) g.newest = proc;
    }
  }
  return [...groups.values()].sort((a, b) => a.component.localeCompare(b.component));
}

function HealthBody({ health }: { health: Health }) {
  const sections = Object.entries(health.sections);
  const alerts = sections.flatMap(([name, s]) => s.alerts.map((a) => ({ ...a, section: name })));
  return (
    <>
      <dl className="facts">
        {sections.map(([name, s]) => (
          <div key={name} style={{ display: "contents" }}>
            <dt>{name}</dt>
            <dd><StatusTag status={s.status} label={s.status === "no_data" ? "no data" : s.status} /></dd>
          </div>
        ))}
      </dl>
      <h3 className="subhead">Firing alerts</h3>
      {alerts.length === 0 ? (
        <p className="secondary">No alert is firing.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Rule</th><th>Severity</th><th className="r">Observed</th><th className="r">Limit</th><th>Runbook</th></tr></thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.rule_id} data-testid={`alert-${a.rule_id}`}>
                  <td><Tag tone="degraded">{words(a.rule_id)}</Tag><div className="panel-sub">{a.summary}</div></td>
                  <td>{a.severity}</td>
                  <td className="r">{a.observed !== undefined ? decimal(a.observed, 2) : ""}</td>
                  <td className="r">{a.threshold != null ? decimal(a.threshold, 2) : ""}</td>
                  <td className="mono">{a.runbook}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="chart-caption">
        Config versions: SLO {health.config_versions.slo}, alerts {health.config_versions.alerts}.
      </p>
    </>
  );
}

function SourcesTable({ sources }: { sources: SourceHealth[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Source</th><th>State</th><th className="r">Newest observation</th><th className="r">Limit</th><th>Last batch</th><th>Quality</th></tr>
        </thead>
        <tbody>
          {sources.map((src) => (
            <tr key={src.source} data-testid={`source-${src.source}`}>
              <td>{words(src.source)}</td>
              <td>
                {src.state === "stale" && src.age_s !== null ? (
                  <StaleTag ageS={src.age_s} limit={`${src.max_age_hours} h`} />
                ) : src.state === "missing" ? (
                  <Tag tone="stale">No data</Tag>
                ) : (
                  <StatusTag status="fresh" label="Fresh" />
                )}
              </td>
              <td className="r">{src.age_s !== null ? `${duration(src.age_s)} old` : "never ingested"}</td>
              <td className="r">{integer(src.max_age_hours)} h</td>
              <td>
                {src.latest_batch ? (
                  <>retrieved {utc(src.latest_batch.retrieved_at)} <span className="muted mono">{src.latest_batch.series_id}</span></>
                ) : "no batch"}
              </td>
              <td>{src.latest_batch ? `${src.latest_batch.quality_status}, schema v${src.latest_batch.schema_version}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
