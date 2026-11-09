# Orchestrator loop: the Clams `clams watch` daemon.
#
# This is NOT the planner Web UI. The planner is a stock Claudio instance
# (claudio/web) that serves the chat/UI on :8080; the orchestrator is the
# Clams control loop that polls GitHub, plans, dispatches and reviews.
# Conflating the two service names is what issue #10 fixed.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The loop writes its DB, heartbeats and task worktrees; give it writable
# mounts for each (bound in docker-compose.yml).
RUN mkdir -p /app/data /app/data/heartbeats /work && \
    chown -R 65534:65534 /app/data /work

CMD ["clams", "watch"]
