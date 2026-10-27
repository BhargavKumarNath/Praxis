"use client";

import { useState, type FormEvent } from "react";

import { authHeader, describeError, request } from "../lib/api";
import type { Approver } from "../lib/approver";
import { utc, words } from "../lib/format";
import type { ActionRequest, ApiError, Freeze } from "../lib/types";
import { StatusTag } from "./Tag";

const VERB: Record<ActionRequest["kind"], string> = {
  freeze_pricing: "freeze",
  rollback: "rollback",
  promotion: "promotion",
};

/** Paste a token; the server says whose it is. Nothing here grants anything by itself. */
export function ApproverSignIn({ approver }: { approver: Approver }) {
  const [value, setValue] = useState("");
  if (approver.name) {
    return (
      <p className="secondary" data-testid="approver">
        Deciding as <strong>{approver.name}</strong> (checked by the console API).{" "}
        <button type="button" className="quiet" onClick={approver.signOut}>
          Sign out
        </button>
      </p>
    );
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await approver.signIn(value.trim())) setValue("");
  };
  return (
    <form className="approval" onSubmit={submit} aria-label="Approver sign in">
      <div className="row">
        <label className="field">
          Approver token (from PRAXIS_CONSOLE_APPROVERS)
          <input
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            size={34}
          />
        </label>
        <button type="submit" disabled={!value.trim() || approver.checking}>
          Sign in to decide
        </button>
      </div>
      {approver.error ? (
        <p className="refused" role="alert">
          {describeError(approver.error)}{" "}
          <span className="muted mono">{approver.error.correlationId}</span>
        </p>
      ) : (
        <p className="panel-sub">Without a valid token the console is read-only.</p>
      )}
    </form>
  );
}

type Outcome = { kind: "done"; text: string } | { kind: "refused"; error: ApiError } | null;

/**
 * Approve or reject one guarded request. The server re-checks the token, runs the existing
 * approval path (ApprovalDesk) and refuses anything that path refuses.
 */
export function RequestDecision({
  req,
  approver,
  onDecided,
}: {
  req: ActionRequest;
  approver: Approver;
  onDecided: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  if (req.status !== "pending") {
    return (
      <p className="secondary">
        <StatusTag status={req.status} /> by {req.decided_by ?? "nobody"} at {utc(req.decided_at)}
        {req.outcome && "reason" in req.outcome ? `: ${String(req.outcome.reason)}` : ""}
      </p>
    );
  }

  const decide = async (action: "approve" | "reject") => {
    if (!approver.token) return;
    setBusy(true);
    const result = await request<{ request: ActionRequest }>(
      `/requests/${encodeURIComponent(req.request_id)}/${action}`,
      {
        method: "POST",
        headers: { ...authHeader(approver.token), "Content-Type": "application/json" },
        body: action === "reject" ? JSON.stringify({ reason }) : undefined,
      },
    );
    setBusy(false);
    if (result.ok) {
      const done = result.data.request;
      setOutcome({ kind: "done", text: `${words(done.status)} by ${done.decided_by}` });
      onDecided();
    } else {
      setOutcome({ kind: "refused", error: result.error });
    }
  };

  const verb = VERB[req.kind];
  const disabled = !approver.token || busy;
  return (
    <div className="approval" data-testid={`decide-${req.request_id}`}>
      <div className="row">
        <button type="button" className="primary" disabled={disabled} onClick={() => void decide("approve")}>
          Approve {verb}
        </button>
        <label className="field">
          Reason to reject
          <input value={reason} onChange={(e) => setReason(e.target.value)} size={28} />
        </label>
        <button type="button" disabled={disabled || reason.trim().length < 3} onClick={() => void decide("reject")}>
          Reject request
        </button>
      </div>
      {!approver.token ? <p className="panel-sub">Sign in as an approver to decide.</p> : null}
      {outcome?.kind === "done" ? (
        <p className="result" role="status">
          <StatusTag status="completed" label={outcome.text} />
        </p>
      ) : null}
      {outcome?.kind === "refused" ? (
        <p className="result refused" role="alert">
          {describeError(outcome.error)} Correlation id{" "}
          <span className="mono">{outcome.error.correlationId ?? "not returned"}</span>.
        </p>
      ) : null}
    </div>
  );
}

export function LiftFreeze({
  freeze,
  approver,
  onLifted,
}: {
  freeze: Freeze;
  approver: Approver;
  onLifted: () => void;
}) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const lift = async () => {
    if (!approver.token) return;
    setBusy(true);
    const result = await request<{ freeze: Freeze }>(
      `/freezes/${encodeURIComponent(freeze.freeze_id)}/lift`,
      { method: "POST", headers: authHeader(approver.token) },
    );
    setBusy(false);
    if (result.ok) onLifted();
    else setError(result.error);
  };
  return (
    <>
      <button type="button" disabled={!approver.token || busy} onClick={() => void lift()}>
        Lift freeze
      </button>
      {error ? (
        <span className="refused" role="alert">
          {" "}
          {describeError(error)} <span className="mono">{error.correlationId}</span>
        </span>
      ) : null}
    </>
  );
}
