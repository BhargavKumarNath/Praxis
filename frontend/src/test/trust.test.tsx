// Trust states (required_test.md s19): stale indicator, synthetic / external labels, a
// missing input shown as "no data" with its reason (never 0), and the safe error state.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorState } from "../components/ErrorState";
import { PanelFrame } from "../components/PanelFrame";
import { Readout } from "../components/Readout";
import { ViewFrame } from "../components/ViewFrame";
import { SystemPanels } from "../views/SystemView";
import { fetchStub, fig, noData, ok, systemBody } from "./fixtures";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("panels", () => {
  it("shows the server's reason, not a number, when a panel has no data", () => {
    render(
      <PanelFrame panel={noData<{ n: number }>("no message has been dead-lettered")} title="Dead letters">
        {(d) => <span>{d.n}</span>}
      </PanelFrame>,
    );
    const panel = screen.getByRole("region", { name: "Dead letters" });
    expect(within(panel).getByText("no message has been dead-lettered")).toBeTruthy();
    expect(within(panel).getByText("No data")).toBeTruthy();
    expect(within(panel).queryByText("0")).toBeNull();
  });

  it("labels simulator output SYNTHETIC and config data as config", () => {
    render(
      <>
        <PanelFrame panel={ok({ n: 1 })} title="Simulated">{(d) => <span>{d.n}</span>}</PanelFrame>
        <PanelFrame panel={ok({ n: 2 }, "config: configs/console/cost.toml", false)} title="Configured">
          {(d) => <span>{d.n}</span>}
        </PanelFrame>
        <PanelFrame panel={ok({ n: 3 }, "duckdb: raw.external_signals", false)} title="Sourced">
          {(d) => <span>{d.n}</span>}
        </PanelFrame>
      </>,
    );
    expect(within(screen.getByRole("region", { name: "Simulated" })).getByText("Synthetic")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Configured" })).getByText("Config")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Sourced" })).getByText("External")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Sourced" })).queryByText("Synthetic")).toBeNull();
  });

  it("renders a missing figure as NO DATA with its reason, and a measured zero as 0", () => {
    render(
      <Readout
        items={[
          { label: "Queue backlog", figure: fig(null, "the Pub/Sub emulator exposes no backlog"), format: String },
          { label: "Dead letters", figure: fig(0), format: String },
        ]}
      />,
    );
    const missing = screen.getByTestId("readout-Queue backlog");
    expect(within(missing).getByText("No data")).toBeTruthy();
    expect(within(missing).getByText("the Pub/Sub emulator exposes no backlog")).toBeTruthy();
    expect(within(missing).queryByText("0")).toBeNull();
    expect(within(screen.getByTestId("readout-Dead letters")).getByText("0")).toBeTruthy();
  });
});

describe("system view", () => {
  it("marks a stale external source with its age and a missing one as no data", () => {
    render(<SystemPanels data={systemBody()} refreshing={false} />);
    expect(within(screen.getByTestId("source-fred")).getByText("Stale 3h 12m")).toBeTruthy();
    expect(within(screen.getByTestId("source-eia")).getByText("No data")).toBeTruthy();
    expect(within(screen.getByTestId("source-eia")).getByText("never ingested")).toBeTruthy();
  });

  it("names stale processes and keeps the cost labelled as an estimate", () => {
    render(<SystemPanels data={systemBody()} refreshing={false} />);
    expect(within(screen.getByTestId("process-consumer-old")).getByText(/^Stale/)).toBeTruthy();
    expect(screen.getByText(/left out of every number above: consumer-old/)).toBeTruthy();
    const cost = screen.getByRole("region", { name: "Cloud cost" });
    expect(within(cost).getByText("Estimate")).toBeTruthy();
    expect(within(cost).getByText(/not a metered cost/)).toBeTruthy();
    expect(within(cost).getAllByText("not estimated").length).toBeGreaterThan(0);
  });

  it("shows a firing alert with its observed value and limit", () => {
    render(<SystemPanels data={systemBody()} refreshing={false} />);
    const alert = screen.getByTestId("alert-price_volatility");
    expect(within(alert).getByText("0.80")).toBeTruthy();
    expect(within(alert).getByText("0.50")).toBeTruthy();
  });
});

describe("view life cycle", () => {
  function Probe() {
    return (
      <ViewFrame<{ generated_at: string; value: number }> path="/probe" title="Probe" deck="d" what="the probe">
        {(d) => <p data-testid="value">{d.value}</p>}
      </ViewFrame>
    );
  }

  it("keeps the last good numbers but marks the view STALE and shows the correlation id when a refresh fails", async () => {
    let fail = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        fetchStub({
          "/probe": () =>
            fail
              ? { status: 503, body: { error: "store_unavailable", correlation_id: "cid-503" } }
              : { body: { generated_at: "2026-10-27T12:00:00+00:00", value: 42 } },
        }),
      ),
    );
    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("value").textContent).toBe("42"));
    expect(screen.queryByText(/^Stale/)).toBeNull();

    fail = true;
    fireEvent.click(screen.getByRole("button", { name: "Read now" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByTestId("correlation-id").textContent).toBe("cid-503");
    expect(screen.getByText("A store behind this view did not answer.")).toBeTruthy();
    expect(screen.getByTestId("value").textContent).toBe("42"); // last good render kept
    expect(screen.getByText(/^Stale/)).toBeTruthy();
  });

  it("marks the view STALE once its last good read is older than two refresh intervals", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["Date", "setInterval", "clearInterval"] });
    // Refreshes hang (never resolve), so the only read is the first one.
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls += 1;
        if (calls > 1) return new Promise<Response>(() => {});
        return fetchStub({ "/probe": () => ({ body: { generated_at: "2026-10-27T12:00:00+00:00", value: 7 } }) })(input);
      }),
    );
    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("value").textContent).toBe("7"));
    expect(screen.queryByText(/^Stale/)).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(61_000);
    });
    expect(screen.getByText(/^Stale 1m 0[01]s$/)).toBeTruthy();
  });

  it("shows a safe error with the correlation id and no server detail it does not recognise", () => {
    render(
      <ErrorState
        error={{ status: 500, error: "internal_error", correlationId: "cid-500", detail: null }}
        what="the pricing view"
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("Could not read the pricing view");
    expect(screen.getByTestId("correlation-id").textContent).toBe("cid-500");
    expect(screen.getByRole("alert").textContent).not.toMatch(/Traceback|Exception/);
  });
});
