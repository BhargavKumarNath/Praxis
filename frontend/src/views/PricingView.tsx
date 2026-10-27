"use client";

import { useState } from "react";

import { CandidateChart } from "../components/charts/CandidateChart";
import { LineChart } from "../components/charts/LineChart";
import { ErrorState } from "../components/ErrorState";
import { PanelFrame } from "../components/PanelFrame";
import { ShadowTag, StatusTag, Tag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { compact, decimal, integer, micros, percent, signedMoney, utc, words } from "../lib/format";
import type { DecisionDetail, LatestDecision, PricingBody } from "../lib/types";
import { useResource } from "../lib/useResource";

// Objective values are micro-GBP per day (praxis.pricing.objective).
const perDay = (v: number) => signedMoney(v / 1e6, Math.abs(v) < 1e7 ? 2 : 0);
const price = (m: number | null) => (m === null ? "not set" : micros(m));

export function PricingView() {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <ViewFrame<PricingBody>
      path="/pricing"
      title="Pricing"
      what="the pricing view"
      deck="What the optimiser would change, product by product, and why. Every decision here was recorded in shadow mode: nothing on this page changes a price, and the console has no way to set one."
      tags={<ShadowTag />}
    >
      {(data, refreshing) => (
        <PricingPanels data={data} refreshing={refreshing} selected={selected} onSelect={setSelected} />
      )}
    </ViewFrame>
  );
}

export function PricingPanels({
  data,
  refreshing,
  selected,
  onSelect,
}: {
  data: PricingBody;
  refreshing: boolean;
  selected: string | null;
  onSelect: (decisionId: string) => void;
}) {
  const p = data.panels;
  const counts = p.status_counts.data?.counts ?? [];
  const total = counts.reduce((n, c) => n + c.n, 0);
  const latest = p.latest.data?.products ?? [];
  const changes = latest.filter((d) => d.chosen_price_micros !== null && d.chosen_price_micros !== d.current_price_micros);
  return (
    <>
      <p className="notice" data-testid="shadow-note">
        {data.note}
      </p>
      <div className="grid section">
        <PanelFrame panel={p.latest} title="Latest cycle" className="span-12" refreshing={refreshing}
          sub={latest[0] ? `Cycle of ${latest[0].as_of}: ${changes.length} of ${latest.length} products would change price` : undefined}
          extra={<ShadowTag />}>
          {(d) => <LatestTable decisions={d.products} selected={selected} onSelect={onSelect} />}
        </PanelFrame>

        {selected ? <DecisionPanel key={selected} decisionId={selected} /> : null}

        <PanelFrame panel={p.history} title="Price over cycles" className="span-8" refreshing={refreshing}
          sub="Current price at each recorded cycle; a proposed change would appear as a step">
          {(d) => (
            <div className="multiples">
              {Object.entries(d.products).map(([product, points]) => (
                <figure key={product} className="multiple">
                  <figcaption>{words(product)}</figcaption>
                  <LineChart
                    title={`${words(product)}: current price per cycle`}
                    height={110}
                    maxGapDays={8}
                    format={(v) => micros(v)}
                    points={points.map((h) => ({ day: h.as_of, value: h.chosen_price_micros ?? h.current_price_micros }))}
                  />
                </figure>
              ))}
            </div>
          )}
        </PanelFrame>

        <PanelFrame panel={p.status_counts} title="Outcomes" className="span-4" refreshing={refreshing}
          sub={`${integer(total)} decisions recorded`}>
          {(d) => (
            <table>
              <thead><tr><th>Outcome</th><th>Mode</th><th className="r">Decisions</th><th className="r">Share</th></tr></thead>
              <tbody>
                {d.counts.map((c) => (
                  <tr key={`${c.status}-${c.mode}`}>
                    <td><StatusTag status={c.status} label={words(c.status)} /></td>
                    <td>{c.mode}</td>
                    <td className="r">{integer(c.n)}</td>
                    <td className="r">{percent(c.n / total, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </PanelFrame>

        <PanelFrame panel={p.recent} title="Decision log" className="span-12" refreshing={refreshing}
          sub="Newest first, from the decision audit table">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Cycle</th><th>Product</th><th>Outcome</th><th className="r">Current</th><th className="r">Proposed</th><th>Reasons</th><th>Policy</th></tr></thead>
                <tbody>
                  {d.decisions.slice(0, 20).map((r) => (
                    <tr key={r.decision_id} className="link-row" data-selected={r.decision_id === selected} onClick={() => onSelect(r.decision_id)}>
                      <td>{r.as_of}</td>
                      <td>{words(r.product)}</td>
                      <td><StatusTag status={r.status} label={words(r.status)} /></td>
                      <td className="r">{price(r.current_price_micros)}</td>
                      <td className="r">{r.chosen_price_micros === null ? <span className="muted">no change</span> : price(r.chosen_price_micros)}</td>
                      <td>{r.reason_codes.map(words).join(", ")}</td>
                      <td className="mono muted">{r.policy_version}</td>
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

function LatestTable({ decisions, selected, onSelect }: { decisions: LatestDecision[]; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Product</th><th>Outcome</th><th className="r">Current</th><th className="r">Proposed</th>
            <th className="r">Change in objective, per day</th><th className="r">P(improvement)</th>
            <th className="r">Demand forecast, per day</th><th>Elasticity by segment</th><th>Guardrails</th><th>Reasons</th><th />
          </tr>
        </thead>
        <tbody>
          {decisions.map((d) => {
            const pr = d.prediction;
            return (
              <tr key={d.decision_id} data-selected={d.decision_id === selected} data-testid={`latest-${d.product}`}>
                <td>{words(d.product)}</td>
                <td><StatusTag status={d.status} label={words(d.status)} /></td>
                <td className="r">{price(d.current_price_micros)}</td>
                <td className="r">{d.chosen_price_micros === null ? <span className="muted">no change</span> : price(d.chosen_price_micros)}</td>
                <td className="r">
                  {pr ? <>{perDay(pr.delta_objective_mean)} <span className="muted">({perDay(pr.delta_objective_p05)} to {perDay(pr.delta_objective_p95)})</span></> : <span className="muted">not predicted</span>}
                </td>
                <td className="r">{pr ? percent(pr.prob_improvement, 0) : ""}</td>
                <td className="r" style={{ whiteSpace: "nowrap" }}>
                  {pr ? `${compact(pr.expected_demand)} units` : <span className="muted">not predicted</span>}
                  {d.forecast_relative_width === null ? null : (
                    <div className="muted">±{percent(d.forecast_relative_width / 2, 0)}</div>
                  )}
                </td>
                <td data-testid={`elasticity-${d.product}`}>
                  {d.elasticity
                    ? Object.entries(d.elasticity).map(([seg, e]) => (
                        <div key={seg} className="num" style={{ whiteSpace: "nowrap" }}>
                          {seg} {decimal(e.mean, 2)} <span className="muted">± {decimal(e.sd, 2)}</span>
                        </div>
                      ))
                    : <span className="muted">not estimated</span>}
                </td>
                <td>{d.guardrails_passed === null ? <span className="muted">not run</span> : d.guardrails_passed ? <StatusTag status="ok" label="Passed" /> : <StatusTag status="failed" label="Failed" />}</td>
                <td>{d.reason_codes.map(words).join(", ")}</td>
                <td><button type="button" className="quiet" onClick={() => onSelect(d.decision_id)}>Why</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DecisionPanel({ decisionId }: { decisionId: string }) {
  const res = useResource<DecisionDetail>(`/pricing/decisions/${encodeURIComponent(decisionId)}`, 0);
  return (
    <section className="panel span-12 decision" aria-label="Decision detail" data-testid="decision-detail">
      {res.error ? <ErrorState error={res.error} what="this decision" onRetry={res.reload} /> : null}
      {res.data ? <DecisionBody detail={res.data} /> : res.loading ? <p className="muted">Reading the decision…</p> : null}
    </section>
  );
}

export function DecisionBody({ detail }: { detail: DecisionDetail }) {
  const d = detail.decision;
  const checks = Object.entries(d.guardrails.checks ?? {});
  const rejected = new Map<string, number>();
  for (const r of d.rejected_alternatives) for (const reason of r.reasons) rejected.set(reason, (rejected.get(reason) ?? 0) + 1);
  const versions = Object.entries(d.lineage).filter(([k]) => k.endsWith("_version"));
  const snapshots = Object.entries(d.lineage).filter(([k]) => !k.endsWith("_version"));
  return (
    <>
      <header className="panel-head">
        <div>
          <h2>{words(d.product)}, cycle of {d.as_of}</h2>
          <p className="panel-sub">
            {words(d.status)}: {d.reason_codes.map(words).join(", ") || "no reason recorded"}. Current {price(d.current_price_micros)}, proposed {d.chosen_price_micros === null ? "no change" : price(d.chosen_price_micros)}.
          </p>
        </div>
        <div className="tags" style={{ display: "flex", gap: 8 }}>
          {d.is_synthetic ? <Tag tone="synthetic">Synthetic</Tag> : null}
          <ShadowTag />
          {d.mode !== "shadow" ? <Tag tone="neutral">{d.mode}</Tag> : null}
        </div>
      </header>
      <p className="notice" style={{ marginTop: 0 }}>{detail.note}</p>
      <div className="grid">
        <div className="span-8">
          <h3 className="subhead">What the optimiser saw</h3>
          <CandidateChart
            candidates={d.candidates}
            current={d.current_price_micros}
            chosen={d.chosen_price_micros}
            formatPrice={micros}
            formatValue={perDay}
          />
          {rejected.size ? (
            <p className="chart-caption">
              Ruled out: {[...rejected.entries()].map(([r, n]) => `${n} by ${words(r)}`).join(", ")}.
            </p>
          ) : null}
        </div>
        <div className="span-4">
          <h3 className="subhead">Guardrails {d.guardrails.passed === undefined ? "" : d.guardrails.passed ? "passed" : "failed"}</h3>
          <ul className="checks">
            {checks.map(([name, passed]) => (
              <li key={name} data-testid={`check-${name}`}>
                <StatusTag status={passed ? "ok" : "failed"} label={passed ? "pass" : "fail"} /> {words(name)}
              </li>
            ))}
          </ul>
          <h3 className="subhead">Model versions</h3>
          <dl className="facts" data-testid="model-versions">
            {versions.map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{words(k.replace(/_version$/, ""))}</dt>
                <dd className="mono">{v}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="span-6">
          <h3 className="subhead">Constraints</h3>
          <dl className="facts">
            {Object.entries(d.constraints).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{words(k.replace(/_micros$/, ""))}</dt>
                <dd className="num">{constraintValue(k, v)}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="span-6">
          <h3 className="subhead">Lineage</h3>
          <dl className="facts">
            {snapshots.map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{words(k)}</dt>
                <dd className="mono">{v}</dd>
              </div>
            ))}
            <dt>Policy</dt>
            <dd className="mono">{d.policy_version}</dd>
            <dt>Decision</dt>
            <dd className="mono">{d.decision_id}</dd>
            <dt>Record SHA-256</dt>
            <dd className="mono">{detail.record_sha256}</dd>
            <dt>Trace</dt>
            <dd className="mono">{detail.trace_id ?? "not recorded"}</dd>
            <dt>Recorded</dt>
            <dd>{utc(detail.recorded_at)}</dd>
            <dt>Executed</dt>
            <dd>{detail.execution ? `by ${detail.execution.executed_by}, ${utc(detail.execution.executed_at)}` : "never (shadow)"}</dd>
          </dl>
          {d.errors.length ? <p className="refused">Errors: {d.errors.join("; ")}</p> : null}
        </div>
      </div>
    </>
  );
}

function constraintValue(key: string, v: unknown): string {
  if (typeof v === "number") {
    if (key.endsWith("_micros")) return micros(v);
    return Number.isInteger(v) ? integer(v) : decimal(v, 3);
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.map((x) => (typeof x === "number" && key.endsWith("_micros") ? micros(x) : String(x))).join(" to ");
  if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${k} ${x}`).join(", ");
  return String(v);
}
