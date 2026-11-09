FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# wdctl is installed by the package (console_scripts)
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD ["wdctl", "selfcheck"]

CMD ["wdctl", "run"]
