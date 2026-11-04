# syntax=docker/dockerfile:1.7
# The decision console UI (Next standalone). It is the ingress container of the PRIVATE
# console service; /api/console/* is proxied to the console API sidecar on loopback, a
# destination fixed at build time (next.config.ts rewrites), so it can never point elsewhere.
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1 PRAXIS_CONSOLE_API_URL=http://127.0.0.1:8001 NEXT_OUTPUT=standalone
RUN npm run build

FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
ARG REVISION=unknown
LABEL org.opencontainers.image.title="praxis-console-web" org.opencontainers.image.revision="${REVISION}"
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=8080
WORKDIR /web
COPY --from=build --chown=node:node /web/.next/standalone ./
COPY --from=build --chown=node:node /web/.next/static ./.next/static
USER node
EXPOSE 8080
HEALTHCHECK NONE
CMD ["node", "server.js"]
