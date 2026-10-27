import { defineConfig } from "vitest/config";

// Component tests run in jsdom against fixture payloads shaped like the console API's
// responses. Fixtures live only under src/test/ (never imported by app code; see
// hygiene.test.ts).
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
  },
});
