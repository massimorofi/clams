# Clams v2 — multi-stage: build the SPA with node, run on python slim.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config
RUN pip install --no-cache-dir . \
    # evidence-gate run commands execute here: pytest plus claudio's own
    # backend/core deps (see claudio-backend|core/pyproject.toml) so the
    # suites the briefs run can import and collect
    && pip install --no-cache-dir pytest pytest-asyncio aiosqlite croniter \
       "google-adk[openai]>=2.0,<3" mcp fastapi "uvicorn[standard]" pyyaml pypdf httpx
COPY --from=web /web/dist ./frontend/dist
COPY clamsctl /usr/local/bin/clamsctl
RUN chmod +x /usr/local/bin/clamsctl
ENTRYPOINT ["clams"]
CMD ["--help"]
