FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The executor writes into /work (task worktrees) and /tmp only.
RUN mkdir -p /work && chown 65534:65534 /work

# `clams worker`, not `clams execute`: the latter is a one-shot that exits 1 on
# an empty queue, which under a restart policy looks exactly like a crash loop.
CMD ["clams", "worker"]
