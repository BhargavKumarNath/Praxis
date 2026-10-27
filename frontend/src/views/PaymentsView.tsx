"use client";

import { DayBars } from "../components/charts/DayBars";
import { PanelFrame } from "../components/PanelFrame";
import { StatusTag } from "../components/Tag";
import { ViewFrame } from "../components/ViewFrame";
import { decimal, integer, money, percent, utc, words } from "../lib/format";
import type { CurrencySummary, PaymentsBody } from "../lib/types";

// recovered: done; closed: written off; suspended: retries stopped, still open; the rest are
// still being retried.
const CASE_TONE: Record<string, string> = {
  recovered: "ok",
  closed: "resolved",
  suspended: "degraded",
  grace: "pending",
  retrying: "pending",
};

export function PaymentsView() {
  return (
    <ViewFrame<PaymentsBody>
      path="/payments"
      title="Payments"
      what="the payments view"
      deck="Failed payments, the dunning cases they open, and how many are recovered. Every retry is scheduled by the recovery policy and charged through the payment gateway; this page only reads."
    >
      {(data, refreshing) => <PaymentsPanels data={data} refreshing={refreshing} />}
    </ViewFrame>
  );
}

export function PaymentsPanels({ data, refreshing }: { data: PaymentsBody; refreshing: boolean }) {
  const p = data.panels;
  return (
    <>
      <p className="notice">{data.note}</p>
      <div className="grid section">
        <PanelFrame panel={p.summary} title="Recovery" className="span-12" refreshing={refreshing}
          sub="All dunning cases, by currency">
          {(d) => (
            <>
              {d.by_currency.map((c) => <CurrencyReadout key={c.currency} c={c} />)}
              <h3 className="subhead">Case states</h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>State</th><th className="r">Cases</th><th className="r">Amount</th></tr></thead>
                  <tbody>
                    {d.states.map((s) => (
                      <tr key={`${s.state}-${s.currency}`}>
                        <td><StatusTag status={CASE_TONE[s.state] ?? "neutral"} label={words(s.state)} /></td>
                        <td className="r">{integer(s.n)}</td>
                        <td className="r">{money(s.amount_minor, s.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </PanelFrame>

        <PanelFrame panel={p.daily} title="Cases by day" className="span-8" refreshing={refreshing}
          sub="Opened and recovered per day (simulated event time)">
          {(d) => (
            <DayBars
              title="Dunning cases opened and recovered per day"
              base="opened"
              outcome="recovered"
              days={d.days.map((x) => ({ day: x.day, base: x.opened, outcome: x.recovered }))}
            />
          )}
        </PanelFrame>

        <PanelFrame panel={p.retries} title="Retries" className="span-4" refreshing={refreshing}
          sub="Scheduled retry jobs (Cloud Tasks abstraction)">
          {(d) => (
            <>
              <table>
                <thead><tr><th>State</th><th className="r">Jobs</th></tr></thead>
                <tbody>
                  {d.states.map((s) => (
                    <tr key={s.state}><td>{words(s.state)}</td><td className="r">{integer(s.n)}</td></tr>
                  ))}
                </tbody>
              </table>
              <h3 className="subhead">Next scheduled</h3>
              {d.upcoming.length === 0 ? (
                <p className="secondary">No retry is scheduled.</p>
              ) : (
                <table>
                  <tbody>
                    {d.upcoming.map((u) => (
                      <tr key={u.job_id}><td className="mono">{u.invoice_id}</td><td>attempt {u.attempt_number}</td><td>{utc(u.run_at)}</td></tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </PanelFrame>

        <PanelFrame panel={p.policies} title="Who decided the retries" className="span-12" refreshing={refreshing}
          sub="The recovery model decides when it is usable; otherwise the deterministic baseline does, and the fallback is recorded">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Policy</th><th>Version</th><th>Model version</th><th className="r">Decisions</th><th className="r">Baseline fallbacks</th><th className="r">Mean P(next succeeds)</th><th>Last decided</th></tr></thead>
                <tbody>
                  {d.policies.map((x) => (
                    <tr key={`${x.policy_kind}-${x.policy_version}-${x.model_version}`} data-testid={`policy-${x.policy_kind}`}>
                      <td>{words(x.policy_kind)}</td>
                      <td className="mono">{x.policy_version}</td>
                      <td className="mono">{x.model_version ?? <span className="muted">none (baseline)</span>}</td>
                      <td className="r">{integer(x.decisions)}</td>
                      <td className="r">{integer(x.fallbacks)}</td>
                      <td className="r">{x.mean_p_next_success === null ? <span className="muted">not predicted</span> : percent(x.mean_p_next_success, 0)}</td>
                      <td>{utc(x.last_decided_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </PanelFrame>

        <PanelFrame panel={p.open_cases} title="Open cases" className="span-12" refreshing={refreshing}
          sub="Newest decision per case">
          {(d) => (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Invoice</th><th>Customer</th><th>State</th><th className="r">Amount</th><th>First failure</th><th className="r">Attempts</th><th>Next action</th><th className="r">P(next retry succeeds)</th><th>Decided by</th></tr></thead>
                <tbody>
                  {d.cases.slice(0, 25).map((c) => (
                    <tr key={c.invoice_id}>
                      <td className="mono">{c.invoice_id}</td>
                      <td className="mono">{c.customer_id}</td>
                      <td>{words(c.state)}</td>
                      <td className="r">{money(c.amount_minor, c.currency)}</td>
                      <td>{c.first_reason ? words(c.first_reason) : ""}<div className="panel-sub">{utc(c.opened_at)}</div></td>
                      <td className="r">{c.attempts_seen}</td>
                      <td>{c.action ? words(c.action) : "none"}{c.next_retry_at ? <div className="panel-sub">{utc(c.next_retry_at)}</div> : null}</td>
                      <td className="r">{c.p_next_success === null ? <span className="muted">not predicted</span> : percent(c.p_next_success, 0)}</td>
                      <td>
                        {c.policy_kind ? words(c.policy_kind) : ""}
                        {c.fallback_reason ? <div className="panel-sub">fallback: {words(c.fallback_reason)}</div> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {d.cases.length > 25 ? <p className="chart-caption">25 of {d.cases.length} shown.</p> : null}
            </div>
          )}
        </PanelFrame>

        <PanelFrame panel={p.invoices} title="Invoices" className="span-6" refreshing={refreshing}>
          {(d) => (
            <table>
              <thead><tr><th>State</th><th className="r">Invoices</th><th className="r">Amount</th></tr></thead>
              <tbody>
                {d.states.map((s) => (
                  <tr key={`${s.state}-${s.currency}`}><td>{words(s.state)}</td><td className="r">{integer(s.n)}</td><td className="r">{money(s.amount_minor, s.currency)}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </PanelFrame>
      </div>
    </>
  );
}

/** Recovered out of ALL cases first; the resolved-only rate only with its definition and the
 * number of cases it leaves out, so a rate over a handful of resolved cases cannot pass for
 * the recovery rate. */
export function CurrencyReadout({ c }: { c: CurrencySummary }) {
  const resolved = c.recovered + c.closed;
  return (
    <div className="readout" style={{ ["--cells" as string]: 4, marginTop: 0 }} data-testid={`recovery-${c.currency}`}>
      <div className="readout-cell hero">
        <span className="readout-label">Recovered, of all cases</span>
        <span className="readout-value">{c.recovered} <span className="unit">of {integer(c.cases)} ({percent(c.recovered / c.cases, 0)})</span></span>
        <div className="readout-compare">{money(c.recovered_minor, c.currency)} of {money(c.failed_minor, c.currency)} failed</div>
      </div>
      <div className="readout-cell">
        <span className="readout-label">Still open</span>
        <span className="readout-value">{integer(c.open_cases)}</span>
        <div className="readout-compare">{money(c.open_amount_minor, c.currency)} outstanding</div>
      </div>
      <div className="readout-cell">
        <span className="readout-label">Recovery rate, resolved cases only</span>
        {c.resolved_recovery_rate === null ? (
          <p className="readout-reason">{c.rate_reason}</p>
        ) : (
          <>
            <span className="readout-value">{percent(c.resolved_recovery_rate, 0)}</span>
            <div className="readout-compare">
              recovered ÷ (recovered + closed), {integer(resolved)} cases; leaves out {integer(c.open_cases)} open
            </div>
          </>
        )}
      </div>
      <div className="readout-cell">
        <span className="readout-label">Time to recover</span>
        {c.ttr_p50_hours === null ? (
          <p className="readout-reason">no case has been recovered</p>
        ) : (
          <>
            <span className="readout-value">{decimal(c.ttr_p50_hours, 0)} h <span className="unit">median</span></span>
            <div className="readout-compare">p90 {decimal(c.ttr_p90_hours ?? 0, 0)} h</div>
          </>
        )}
      </div>
    </div>
  );
}
