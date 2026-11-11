# NomadReady on Cloud Run. Built by cloudbuild.yaml, which first copies data/nomad.duckdb from the
# private GCS bucket into the build folder (the data isn't in the repo), so it's baked into the image
# and nothing is downloaded at startup.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Dependencies first, so code-only changes reuse this layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app.py ./
COPY scout ./scout
COPY tools ./tools
COPY static ./static
COPY data/nomad.duckdb ./data/nomad.duckdb

# Run as a non-root user; the database is opened read-only
RUN useradd --system --uid 1001 app && chown -R app /app/data
USER app

# Cloud Run sets $PORT
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
