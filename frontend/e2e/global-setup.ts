import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { E2E_DB } from "../playwright.config";

// A fresh control plane, then the key journey through the real code paths
// (tests/e2e/seed_journey.py): a pricing snapshot over the volatility limit -> the ops monitor
// opens the incident -> deterministic triage requests a freeze (pending, nothing frozen).
export default function globalSetup(): void {
  const root = join(__dirname, "..", "..");
  const python = join(root, ".venv", "bin", "python");
  const admin = E2E_DB.replace(/\/praxis_console_e2e$/, "/postgres");
  execFileSync(
    python,
    [
      "-c",
      "import sys; from sqlalchemy import create_engine, text; " +
        "c = create_engine(sys.argv[1], isolation_level='AUTOCOMMIT').connect(); " +
        "c.execute(text('DROP DATABASE IF EXISTS praxis_console_e2e WITH (FORCE)')); " +
        "c.execute(text('CREATE DATABASE praxis_console_e2e'))",
      admin,
    ],
    { cwd: root, stdio: "inherit" },
  );
  const out = execFileSync(python, ["-m", "tests.e2e.seed_journey", "--database-url", E2E_DB], {
    cwd: root,
    env: { ...process.env, PRAXIS_LOG_LEVEL: "WARNING" },
  }).toString();
  writeFileSync(join(__dirname, "..", "test-results-journey.json"), out.trim().split("\n").at(-1) ?? "{}");
}
