FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app/backend

# Dependencies first so code changes do not invalidate this layer.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
RUN uv sync --frozen --no-dev

# The approved documents and sources.yaml are mounted at /app/data (see docker-compose.yml).
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app
USER app
