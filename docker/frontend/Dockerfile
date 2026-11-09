# Clams web frontend + API.
#
# Multi-stage: the SPA is built with Node, then served by the Clams API process
# (which mounts frontend/dist as static files), so production is a single
# container and a single origin -- no CORS, no second web server.

# ---- build the SPA -----------------------------------------------------
FROM node:22-alpine AS webbuild

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- runtime ---------------------------------------------------------
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The built SPA must sit at <repo>/frontend/dist relative to the installed
# package for the API's static mount to find it.
COPY --from=webbuild /web/dist /app/frontend/dist

RUN mkdir -p /app/data /app/data/heartbeats && \
    chown -R 65534:65534 /app/data

EXPOSE 8200

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8200/healthz || exit 1

CMD ["clams", "serve"]
