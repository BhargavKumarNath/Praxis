import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

// The key operational journey, through the shipped app: an alerting signal -> its incident ->
// the investigation trace from the audit log -> the pending request -> a refused token ->
// an authorized approval by the approver the SERVER names -> the freeze is active.
const journey = JSON.parse(readFileSync(join(__dirname, "..", "test-results-journey.json"), "utf8")) as {
  incident_id: string;
  session_id: string;
  request_id: string;
};
const token = process.env.PRAXIS_E2E_TOKEN!;

test("an alerting signal leads to an incident, its trace, a pending request and an authorized approval", async ({ page }) => {
  // 1. the signal: the alert fires on the System view, labelled with observed value and limit
  await page.goto("/system");
  const alert = page.getByTestId("alert-price_volatility");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("0.80");
  await expect(alert).toContainText("0.50");
  await expect(page.getByText("Synthetic").first()).toBeVisible();

  // 2. the incident, opened by the ops monitor
  await page.getByRole("link", { name: /AI operator/ }).click();
  await expect(page.getByTestId("investigator-note")).toContainText("not an LLM");
  const incident = page.getByTestId("incident-price_volatility");
  await expect(incident).toContainText("open");
  await incident.getByRole("button", { name: "price volatility" }).click();

  // 3. the investigation trace, from the audit log, ending in the guarded request
  const trace = page.getByTestId("trace");
  await expect(trace).toContainText(journey.session_id);
  await expect(trace).toContainText("get_system_health");
  await expect(trace).toContainText("freeze_pricing");
  await expect(trace).toContainText("Diagnosis price volatility");

  // 4. the pending request cannot be decided without an approver
  const decide = page.getByTestId(`decide-${journey.request_id}`).first();
  await expect(decide.getByRole("button", { name: "Approve freeze" })).toBeDisabled();
  await expect(page.getByTestId("request-freeze_pricing")).toContainText("pending");

  // 5. a token the server does not accept is refused, with a correlation id
  await page.getByLabel(/Approver token/).fill(`${token}-wrong`);
  await page.getByRole("button", { name: "Sign in to decide" }).click();
  await expect(page.getByText("The approver token was not accepted.")).toBeVisible();
  await expect(decide.getByRole("button", { name: "Approve freeze" })).toBeDisabled();

  // 6. the authorized approval: the server names the approver; the existing path runs
  await page.getByLabel(/Approver token/).fill(token);
  await page.getByRole("button", { name: "Sign in to decide" }).click();
  await expect(page.getByTestId("approver")).toContainText("e2e-approver");
  await decide.getByRole("button", { name: "Approve freeze" }).click();
  await expect(decide.getByRole("status")).toContainText("completed by e2e-approver");

  // 7. the outcome, read back from the stores of record
  await page.getByRole("button", { name: "Read now" }).click();
  const freeze = page.locator("[data-testid^=freeze-]").first();
  await expect(freeze).toContainText("e2e-approver");
  await expect(freeze).toContainText("Active");
  await expect(page.getByText("No request is waiting.")).toBeVisible();

  // 8. the same request cannot be approved twice (the server refuses; nothing re-runs)
  const again = await page.request.post(`/api/console/requests/${journey.request_id}/approve`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(again.status()).toBe(409);
  expect((await again.json()).correlation_id).toBeTruthy();
});
