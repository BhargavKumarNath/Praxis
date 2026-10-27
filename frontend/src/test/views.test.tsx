// Decision details, model versions, the shadow label and the honest framing of recovery and
// lagging figures (required_test.md s19).

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ExecutivePanels } from "../views/ExecutiveView";
import { MlopsPanels } from "../views/MlopsView";
import { CurrencyReadout } from "../views/PaymentsView";
import { DecisionBody } from "../views/PricingView";
import { currency, decisionDetail, executiveBody, mlopsBody } from "./fixtures";

describe("pricing decision detail", () => {
  it("shows the reasons, guardrail results, constraints and lineage of the recorded decision", () => {
    render(<DecisionBody detail={decisionDetail()} />);
    expect(screen.getByText(/hold: no improvement/)).toBeTruthy();
    expect(within(screen.getByTestId("check-margin_floor")).getByText("fail")).toBeTruthy();
    expect(within(screen.getByTestId("check-capacity")).getByText("pass")).toBeTruthy();
    expect(screen.getByText("Guardrails failed")).toBeTruthy();
    expect(screen.getByText("price floor")).toBeTruthy(); // _micros dropped, value shown in £
    expect(screen.getByText("£0.20")).toBeTruthy();
    expect(screen.getByText("Ruled out: 1 by capacity.")).toBeTruthy();
    expect(screen.getByText("a".repeat(64))).toBeTruthy(); // record checksum
    expect(screen.getByText("never (shadow)")).toBeTruthy();
  });

  it("names every model version the decision used", () => {
    render(<DecisionBody detail={decisionDetail()} />);
    const versions = screen.getByTestId("model-versions");
    expect(within(versions).getByText("demand-hybrid-24c8")).toBeTruthy();
    expect(within(versions).getByText("elasticity-hier-0ff4")).toBeTruthy();
    expect(within(versions).queryByText("market-68")).toBeNull(); // a snapshot, listed under lineage
  });

  it("is labelled shadow: not validated for execution", () => {
    render(<DecisionBody detail={decisionDetail()} />);
    expect(screen.getByText(/shadow: not validated for execution/)).toBeTruthy();
    expect(screen.getByText("Shadow")).toBeTruthy();
    expect(screen.getByText("Synthetic")).toBeTruthy();
  });

  it("has no control that could set or execute a price", () => {
    render(<DecisionBody detail={decisionDetail()} />);
    expect(screen.queryAllByRole("button")).toEqual([]);
    expect(screen.queryAllByRole("textbox")).toEqual([]);
  });
});

describe("pricing latest cycle", () => {
  it("shows the demand forecast and the elasticity with its uncertainty", async () => {
    const { PricingPanels } = await import("../views/PricingView");
    const { pricingBody } = await import("./fixtures");
    render(<PricingPanels data={pricingBody()} refreshing={false} selected={null} onSelect={() => {}} />);
    const row = screen.getByTestId("latest-api_requests");
    expect(row.textContent).toContain("55.8K units");
    expect(screen.getByTestId("elasticity-api_requests").textContent).toBe("growth -1.19 ± 0.07");
    expect(row.textContent).toContain("± 0.07");
    expect(screen.getByTestId("shadow-note").textContent).toContain("not validated for execution");
  });
});

describe("ML operations", () => {
  it("shows each version with stage, validation, data, features, code and checksum", () => {
    render(<MlopsPanels data={mlopsBody()} refreshing={false} />);
    const row = screen.getByTestId("version-demand-hybrid-24c8");
    for (const text of ["champion", "Passed", "panel-1", "demand_features.v1", "0cb9304", "c".repeat(12), "13.5%"]) {
      expect(within(row).getByText(text)).toBeTruthy();
    }
    expect(within(row).getByText(/serving/)).toBeTruthy();
    expect(within(screen.getByTestId("version-demand-hybrid-old")).getByText("not validated")).toBeTruthy();
  });

  it("marks a forced promotion and renders both events of one decision", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<MlopsPanels data={mlopsBody()} refreshing={false} />);
    expect(screen.getAllByTestId("event-promote.forced")).toHaveLength(2);
    expect(screen.getAllByText("Forced")).toHaveLength(2);
    expect(error.mock.calls.filter((c) => String(c[0]).includes("same key"))).toEqual([]);
  });

  it("shows a missing serving signal as no data with its reason", () => {
    render(<MlopsPanels data={mlopsBody()} refreshing={false} />);
    const cell = screen.getByTestId("readout-Calibration, 80% interval");
    expect(within(cell).getByText("No data")).toBeTruthy();
    expect(within(cell).getByText(/model_calibration_80/)).toBeTruthy();
  });
});

describe("payments recovery", () => {
  it("leads with recovered out of all cases, and qualifies the resolved-only rate", () => {
    render(<CurrencyReadout c={currency()} />);
    const r = screen.getByTestId("recovery-GBP");
    expect(r.textContent).toContain("14 of 42 (33%)");
    expect(r.textContent).toContain("100%");
    expect(r.textContent).toContain("leaves out 28 open");
  });

  it("gives the reason when no case is resolved yet", () => {
    render(<CurrencyReadout c={currency({ recovered: 0, resolved_recovery_rate: null, rate_reason: "no case is resolved yet", ttr_p50_hours: null, ttr_p90_hours: null })} />);
    expect(screen.getByText("no case is resolved yet")).toBeTruthy();
    expect(screen.getByText("no case has been recovered")).toBeTruthy();
  });
});

describe("executive", () => {
  it("flags a figure that lags the others and keeps an unbuilt one as unavailable", () => {
    render(<ExecutivePanels data={executiveBody()} refreshing={false} />);
    const price = screen.getByTestId("exec-price_index");
    expect(price.textContent).toContain("57 days behind");
    expect(screen.getByTestId("exec-revenue").textContent).not.toContain("behind");
    const margin = screen.getByRole("region", { name: "Gross margin, usage" });
    expect(within(margin).getByText("Unavailable")).toBeTruthy();
    expect(within(margin).getByText(/make dbt-build/)).toBeTruthy();
  });
});
