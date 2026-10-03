# Minimal image for the Information-Disclosure Hunting Agent.
# Not a production build (this is a take-home assignment) -- single stage,
# no multi-arch matrix, no health checks: just "pip install and run".

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so they're cached separately from source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY creds.example.json .

# Runs as a non-root user -- cheap hygiene, not a hardening exercise.
RUN useradd --create-home --uid 1000 agent && chown -R agent:agent /app
USER agent

# All real configuration is via environment variables / a mounted .env file
# (see .env.example and README.md's "Running with Docker" section) -- e.g.:
#   docker run --rm --env-file .env -v "$(pwd)/creds.json:/app/creds.json" \
#     -v "$(pwd)/out:/app/out" info-disclosure-agent --out-dir /app/out
ENTRYPOINT ["python", "-m", "agent"]
