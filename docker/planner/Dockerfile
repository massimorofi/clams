# Planner runs the Claudio image with the orchestrator's planner profile.
# Build only if you are NOT reusing a prebuilt claudio:latest.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["clams", "watch"]
