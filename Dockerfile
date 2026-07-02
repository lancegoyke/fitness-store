# syntax=docker/dockerfile:1

# Builds the designer React island (Decision 3,
# docs/meso/designer-framework-plan.md) to STABLE, un-hashed filenames; the
# python stage below copies dist/ into the static tree before collectstatic
# (which re-hashes it via WhiteNoise's manifest storage) runs at container
# start. A broken frontend build fails here, so it can never reach prod.
FROM node:22-slim AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend/designer/ ./frontend/designer/
RUN npm run build

# Production image for Mastering Fitness, built on the Hetzner box by the
# `deploy` tool (docker compose build). Build context is the repo root.
# WhiteNoise serves static files from this image; media lives on S3.
FROM python:3.13-slim

# uv for fast, reproducible installs straight from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/app"

WORKDIR /app

# Install dependencies first for layer caching. The project has no build-system
# (it runs from source via PYTHONPATH), so only the locked deps are installed.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application source.
COPY . .

# Designer island build output, in place before the entrypoint's
# `collectstatic` (WhiteNoise re-hashes it into the manifest at container
# start — see the frontend-builder stage above).
COPY --from=frontend-builder /app/app/store_project/static/js/dist/ /app/app/store_project/static/js/dist/

# Non-root runtime user; staticfiles dir must be writable for collectstatic.
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/app/staticfiles \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser
# Run from the Django project dir so `config` is importable and manage.py is local.
WORKDIR /app/app
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# gunicorn reads WEB_CONCURRENCY from the environment for its worker count.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", "--error-logfile", "-"]
