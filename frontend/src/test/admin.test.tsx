// The admin authorization path (required_test.md s19): the UI cannot decide without a token
// the SERVER accepts, the server names the approver, a decision carries only the bearer
// token (never a name), and a refusal is shown with its correlation id.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApproverSignIn, RequestDecision } from "../components/Approval";
import type { Approver } from "../lib/approver";
import { useApprover } from "../lib/approver";
import { IncidentBody, OperatorPanels } from "../views/OperatorView";
import { fetchStub, incidentDetail, operatorBody, pendingRequest } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const TOKEN = ["tok", "en", "-for-tests-only-0123456789"].join(""); // built at runtime

function Harness({ onDecided = () => {} }: { onDecided?: () => void }) {
  const approver = useApprover();
  return (
    <>
      <ApproverSignIn approver={approver} />
      <RequestDecision req={pendingRequest()} approver={approver} onDecided={onDecided} />
    </>
  );
}

function signIn() {
  fireEvent.change(screen.getByLabelText(/Approver token/), { target: { value: TOKEN } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in to decide" }));
}

describe("approver boundary", () => {
  it("keeps decisions disabled without a signed-in approver", () => {
    render(<Harness />);
    expect((screen.getByRole("button", { name: "Approve freeze" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Reject request" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Sign in as an approver to decide.")).toBeTruthy();
  });

  it("refuses a token the server rejects and stays read-only", async () => {
    const fetch = vi.fn(
      fetchStub({ "/whoami": () => ({ status: 401, body: { error: "unauthorized", correlation_id: "cid-401" } }) }),
    );
    vi.stubGlobal("fetch", fetch);
    render(<Harness />);
    signIn();
    await waitFor(() => expect(screen.getByText(/approver token was not accepted/)).toBeTruthy());
    expect(screen.getByText("cid-401")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Approve freeze" }) as HTMLButtonElement).disabled).toBe(true);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("approves as the approver the server names, sending only the bearer token", async () => {
    const fetch = vi.fn(
      fetchStub({
        "/whoami": () => ({ body: { approver: "alice" } }),
        "/requests/req_1/approve": () => ({
          body: { request: pendingRequest({ status: "completed", decided_by: "alice" }) },
        }),
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const onDecided = vi.fn();
    render(<Harness onDecided={onDecided} />);
    signIn();
    await waitFor(() => expect(screen.getByTestId("approver").textContent).toContain("alice"));

    fireEvent.click(screen.getByRole("button", { name: "Approve freeze" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("completed by alice"));
    expect(onDecided).toHaveBeenCalledOnce();

    const [url, init] = fetch.mock.calls.at(-1)!;
    expect(String(url)).toBe("/api/console/requests/req_1/approve");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(init?.body).toBeUndefined(); // the client cannot name the approver
  });

  it("shows a refusal from the approval path with its correlation id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        fetchStub({
          "/whoami": () => ({ body: { approver: "bob" } }),
          "/requests/req_1/approve": () => ({
            status: 409,
            body: { error: "refused", detail: "request req_1 is completed, not pending", correlation_id: "cid-409" },
          }),
        }),
      ),
    );
    render(<Harness />);
    signIn();
    await waitFor(() => expect(screen.getByTestId("approver")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Approve freeze" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("is completed, not pending");
    expect(alert.textContent).toContain("cid-409");
  });

  it("needs a reason to reject and sends it in the body", async () => {
    const fetch = vi.fn(
      fetchStub({
        "/whoami": () => ({ body: { approver: "alice" } }),
        "/requests/req_1/reject": () => ({
          body: { request: pendingRequest({ status: "rejected", decided_by: "alice" }) },
        }),
      }),
    );
    vi.stubGlobal("fetch", fetch);
    render(<Harness />);
    signIn();
    await waitFor(() => expect(screen.getByTestId("approver")).toBeTruthy());
    const reject = screen.getByRole("button", { name: "Reject request" }) as HTMLButtonElement;
    expect(reject.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Reason to reject"), { target: { value: "false alarm" } });
    fireEvent.click(reject);
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("rejected by alice"));
    expect(JSON.parse(String(fetch.mock.calls.at(-1)![1]?.body))).toEqual({ reason: "false alarm" });
  });
});

describe("operator view", () => {
  const readOnly: Approver = { token: null, name: null, error: null, checking: false, signIn: async () => false, signOut: () => {} };

  it("says the investigator is deterministic triage, not an LLM", () => {
    render(<OperatorPanels data={operatorBody()} refreshing={false} approver={readOnly} incidentId={null} onSelect={() => {}} />);
    expect(screen.getByTestId("investigator-note").textContent).toMatch(/deterministic triage.*not an LLM/);
    expect(within(screen.getByRole("region", { name: "Investigator" })).getByText("Not an LLM")).toBeTruthy();
  });

  it("shows the pending request with its rationale, evidence and target in words", () => {
    render(<OperatorPanels data={operatorBody()} refreshing={false} approver={readOnly} incidentId={null} onSelect={() => {}} />);
    const card = screen.getByTestId("request-freeze_pricing");
    expect(within(card).getByText("all products")).toBeTruthy();
    expect(within(card).getByText("price_volatility found by deterministic triage")).toBeTruthy();
    expect(within(card).getByText("call_1")).toBeTruthy();
  });

  it("renders the investigation trace from the audit, in call order", () => {
    render(<IncidentBody detail={incidentDetail()} approver={readOnly} onDecided={() => {}} />);
    const trace = screen.getByTestId("trace");
    const tools = within(trace).getAllByText(/^(get_system_health|request_price_freeze)$/).map((n) => n.textContent);
    expect(tools).toEqual(["get_system_health", "request_price_freeze"]);
    expect(trace.textContent).toContain("price volatility");
  });
});
