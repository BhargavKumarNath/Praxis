import type { NextConfig } from "next";

// The browser only ever talks to this Next server. /api/console/* is proxied to the console
// API (python: praxis.console, `make console-api`), so there is no CORS surface and the
// approver token travels in a header to the same origin.
const consoleApi = process.env.PRAXIS_CONSOLE_API_URL ?? "http://127.0.0.1:8001";

const nextConfig: NextConfig = {
  // The container build (infra/docker/web.Dockerfile) sets NEXT_OUTPUT=standalone; local dev
  // and the E2E journey keep the default output.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  reactStrictMode: true,
  poweredByHeader: false,
  devIndicators: false,
  async rewrites() {
    return [{ source: "/api/console/:path*", destination: `${consoleApi}/v1/console/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
