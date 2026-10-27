"use client";

import { useState } from "react";

import { ApproverSignIn, LiftFreeze, RequestDecision } from "../components/Approval";
import { ErrorState } from "../components/ErrorState";
import { PanelFrame } from "../components/PanelFrame";
import { StatusTag, Tag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { useApprover, type Approver } from "../lib/approver";
import { utc, utcTime, words } from "../lib/format";
import type { ActionRequest, IncidentDetail, OperatorBody, Trace } from "../lib/types";
import { useResource } from "../lib/useResource";

export function OperatorView() {
  const approver = useApprover();
  const [incidentId, setIncidentId] = useState<string | null>(null);
  return (
    <ViewFrame<OperatorBody>
      path="/operator"
      title="AI operator"
      what="the operator view"
      deck="Open incidents, what the investigator found, and the actions it asked a person to approve. The investigator is deterministic triage over typed, audited tools; it cannot set a price, run a payment or bypass a guardrail."
      tags={<Tag tone="neutral">Not an LLM</Tag>}
    >
      {(data, refreshing) => (
        <OperatorPanels
          data={data}
          refreshing={refreshing}
          approver={approver}
          incidentId={incidentId}
          onSelect={setIncidentId}
        />
      )}
    </ViewFrame>
  );
}

export function OperatorPanels({
  data,
  refreshing,
  approver,
  incidentId,
  onSelect,
}: {
  data: OperatorBody;
  refreshing: boolean;
  approver: Approver;
  incidentId: string | null;
  onSelect: (id: string) => void;
}) {
  const p = data.panels;
  const [decided, setDecided] = useState(0);
  return (
    <>
      <p className="notice" data-testid="investigator-note">{data.note}</p>
      <div className="grid section">
        <PanelFrame panel={p.provider} title="Investigator" className="span-5" refreshing={refreshing}>
          {(prov) => (
            <dl className="facts">
              <dt>Provider</dt>
              <dd>{prov.provider} <Tag tone="neutral">{prov.is_llm ? "LLM" : "Not an LLM"}</Tag></dd>
              <dt>Policy</dt>
              <dd className="mono">{prov.policy_version}</dd>
              <dt>Acts as</dt>
              <dd className="mono">{prov.actor}</dd>
              <dt>Highest tier</dt>
              <dd>{prov.max_tier}: tier 2 tools only create pending requests; a named person completes them</dd>
              <dt>Actions per session</dt>
              <dd>at most {prov.max_actions}</dd>
            </dl>
          )}
        </PanelFrame>
        <section className="panel span-7" aria-label="Approver">
          <header className="panel-head">
            <div>
              <h2>Approver</h2>
              <p className="panel-sub">Approvals run the existing paths: ApprovalDesk, FreezeStore, ModelRegistry</p>
            </div>
          </header>
          <ApproverSignIn approver={approver} />
        </section>

        <PanelFrame panel={p.requests} title="Waiting for a decision" className="span-12" refreshing={refreshing}
          sub="Guarded actions the investigator requested. Nothing happens until a named approver decides.">
          {(d) => (
            <RequestList
              requests={d.requests}
              approver={approver}
              onDecided={() => setDecided((n) => n + 1)}
            />
          )}
        </PanelFrame>

        <PanelFrame panel={p.incidents} title="Incidents" className="span-12" refreshing={refreshing}
          sub="Opened by the ops monitor from pre-registered alert rules. Select one for its investigation.">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Incident</th><th>Status</th><th>Severity</th><th className="r">Seen</th><th>Opened</th><th>Runbook</th></tr>
                </thead>
                <tbody>
                  {d.incidents.map((i) => (
                    <tr
                      key={i.incident_id}
                      className="link-row"
                      data-selected={i.incident_id === incidentId}
                      data-testid={`incident-${i.rule_id}`}
                      onClick={() => onSelect(i.incident_id)}
                    >
                      <td>
                        <button type="button" className="quiet" onClick={() => onSelect(i.incident_id)}>
                          {words(i.rule_id)}
                        </button>
                        <div className="panel-sub">{i.summary}</div>
                      </td>
                      <td><StatusTag status={i.status} /></td>
                      <td>{i.severity}</td>
                      <td className="r num">{i.occurrences}×</td>
                      <td>{utc(i.opened_at)}</td>
                      <td className="mono">{i.runbook}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>

        {incidentId ? (
          <IncidentPanel key={`${incidentId}-${decided}`} incidentId={incidentId} approver={approver}
            onDecided={() => setDecided((n) => n + 1)} />
        ) : null}

        <PanelFrame panel={p.freezes} title="Pricing freezes" className="span-6" refreshing={refreshing}
          sub="While a freeze is active the optimiser records decisions but changes no price">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Scope</th><th>Approved by</th><th>Since</th><th>State</th><th /></tr></thead>
                <tbody>
                  {d.active.map((f) => (
                    <tr key={f.freeze_id} data-testid={`freeze-${f.freeze_id}`}>
                      <td>{f.scope}<div className="panel-sub">{f.reason}</div></td>
                      <td>{f.approved_by}</td>
                      <td>{utc(f.started_at)}</td>
                      <td><Tag tone="degraded">Active</Tag></td>
                      <td><LiftFreeze freeze={f} approver={approver} onLifted={() => setDecided((n) => n + 1)} /></td>
                    </tr>
                  ))}
                  {d.lifted.map((f) => (
                    <tr key={f.freeze_id}>
                      <td>{f.scope}</td>
                      <td>{f.approved_by}</td>
                      <td>{utc(f.started_at)}</td>
                      <td className="secondary">lifted by {f.lifted_by}, {utc(f.lifted_at)}</td>
                      <td />
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>

        <PanelFrame panel={p.sessions} title="Investigations" className="span-6" refreshing={refreshing}
          sub="Every session, from the agent audit log">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Started</th><th>Diagnosis</th><th className="r">Calls</th><th className="r">Requests</th><th>Grounded</th></tr></thead>
                <tbody>
                  {d.sessions.map((s) => (
                    <tr key={s.session_id}>
                      <td>{utc(s.started_at)}</td>
                      <td>{s.diagnosis ? words(s.diagnosis) : <span className="muted">{s.outcome ?? "no outcome"}</span>}</td>
                      <td className="r">{s.calls}</td>
                      <td className="r">{s.actions}</td>
                      <td>{s.grounded === null ? "not recorded" : s.grounded ? "yes" : "no"}</td>
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

function RequestList({
  requests,
  approver,
  onDecided,
}: {
  requests: ActionRequest[];
  approver: Approver;
  onDecided: () => void;
}) {
  const pending = requests.filter((r) => r.status === "pending");
  const done = requests.filter((r) => r.status !== "pending");
  return (
    <>
      {pending.length === 0 ? <p className="secondary">No request is waiting.</p> : null}
      {pending.map((r) => (
        <RequestCard key={r.request_id} req={r} approver={approver} onDecided={onDecided} />
      ))}
      {done.length ? (
        <details className="table-view">
          <summary>{done.length} decided earlier</summary>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Action</th><th>Target</th><th>Status</th><th>Decided by</th><th>At</th></tr></thead>
              <tbody>
                {done.map((r) => (
                  <tr key={r.request_id}>
                    <td>{words(r.kind)}</td>
                    <td>{target(r.target)}</td>
                    <td><StatusTag status={r.status} /></td>
                    <td>{r.decided_by}</td>
                    <td>{utc(r.decided_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </>
  );
}

// A freeze scope of "*" is every product; anything else is a product or model id.
function target(t: string): string {
  return t === "*" ? "all products" : t;
}

function RequestCard({ req, approver, onDecided }: { req: ActionRequest; approver: Approver; onDecided: () => void }) {
  return (
    <article className="object" style={{ marginBottom: 12 }} data-testid={`request-${req.kind}`}>
      <div className="panel-head">
        <h3 style={{ margin: 0, fontSize: "var(--t-16)" }}>
          {words(req.kind)} <span className="secondary">{target(req.target)}</span>
        </h3>
        <StatusTag status={req.status} />
      </div>
      <p style={{ margin: "0 0 8px" }}>{req.rationale}</p>
      <dl className="facts">
        <dt>Requested by</dt>
        <dd className="mono">{req.requested_by}, {utc(req.created_at)}</dd>
        <dt>Evidence</dt>
        <dd>{req.evidence.length ? req.evidence.map((e) => <code key={e} style={{ marginRight: 8 }}>{e}</code>) : "none cited"}</dd>
        <dt>Session</dt>
        <dd className="mono">{req.session_id}</dd>
      </dl>
      <div style={{ marginTop: 12 }}>
        <RequestDecision req={req} approver={approver} onDecided={onDecided} />
      </div>
    </article>
  );
}

function IncidentPanel({ incidentId, approver, onDecided }: { incidentId: string; approver: Approver; onDecided: () => void }) {
  const res = useResource<IncidentDetail>(`/incidents/${encodeURIComponent(incidentId)}`, 0);
  return (
    <section className="panel span-12" aria-label="Incident investigation" data-testid="incident-detail">
      {res.error ? <ErrorState error={res.error} what="this incident" onRetry={res.reload} /> : null}
      {res.data ? <IncidentBody detail={res.data} approver={approver} onDecided={onDecided} /> : res.loading ? <p className="muted">Reading the incident…</p> : null}
    </section>
  );
}

export function IncidentBody({ detail, approver, onDecided }: { detail: IncidentDetail; approver: Approver; onDecided: () => void }) {
  const i = detail.incident;
  return (
    <>
      <header className="panel-head">
        <div>
          <h2>{words(i.rule_id)}</h2>
          <p className="panel-sub">{i.summary}. Runbook <span className="mono">{i.runbook}</span></p>
        </div>
        <div className="tags" style={{ display: "flex", gap: 8 }}>
          {i.synthetic ? <Tag tone="synthetic">Synthetic</Tag> : null}
          <StatusTag status={i.status} />
        </div>
      </header>
      <div className="grid">
        <div className="span-4">
          <h3 className="panel-sub">Incident timeline</h3>
          <ol className="trace">
            {detail.events.map((e, n) => (
              <li key={n}>
                <span>{words(e.kind)}</span>
                <time className="muted">{utcTime(e.at)}</time>
              </li>
            ))}
          </ol>
        </div>
        <div className="span-8">
          <h3 className="panel-sub">Investigation trace, from the audit log</h3>
          {detail.investigations.length === 0 ? (
            <p className="secondary">No investigation has run for this incident. Run `python -m praxis.agent investigate`.</p>
          ) : (
            detail.investigations.map((t) => <TraceBlock key={t.session.session_id} trace={t} approver={approver} onDecided={onDecided} />)
          )}
          <p className="chart-caption">{detail.note}</p>
        </div>
      </div>
    </>
  );
}

function TraceBlock({ trace, approver, onDecided }: { trace: Trace; approver: Approver; onDecided: () => void }) {
  const s = trace.session;
  return (
    <div data-testid="trace" style={{ marginBottom: 16 }}>
      <p style={{ margin: "0 0 4px" }}>
        Diagnosis <strong>{s.diagnosis ? words(s.diagnosis) : "none"}</strong>
        <span className="muted">, {s.calls} tool calls, grounded {s.grounded === null ? "not recorded" : s.grounded ? "yes" : "no"}, session <span className="mono">{s.session_id}</span></span>
      </p>
      <ol className="trace">
        {trace.calls.map((c) => (
          <li key={c.call_id}>
            <span><span className="tool mono">{c.tool}</span> <span className="muted">tier {c.tier ?? "?"}</span></span>
            <span>{c.status ? <StatusTag status={c.status === "ok" ? "ok" : c.status} label={c.status} /> : <span className="muted">no outcome</span>}</span>
            <span className="args mono">
              {Object.keys(c.args).length ? JSON.stringify(c.args) : "no arguments"}
              {c.error ? ` | ${c.error}` : ""}
            </span>
          </li>
        ))}
      </ol>
      {trace.requests.map((r) => (
        <div key={r.request_id} style={{ marginTop: 12 }}>
          <p style={{ margin: "0 0 6px" }}>
            Requested <strong>{words(r.kind)}</strong>, {target(r.target)}
          </p>
          <RequestDecision req={r} approver={approver} onDecided={onDecided} />
        </div>
      ))}
    </div>
  );
}
