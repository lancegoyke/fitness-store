#!/bin/sh
set -e

# Runs before the web process starts. The deploy tool invokes migrations
# separately as `... run --rm web sh -c 'python manage.py migrate'` (where
# $1 = "sh"), which falls straight through to exec without re-running these.
# When the container starts gunicorn ($1 = "gunicorn") we (idempotently)
# migrate and collect static — collectstatic MUST run here because WhiteNoise's
# ManifestStaticFilesStorage 500s on any asset missing from a fresh manifest,
# and the deploy script does not run it.
if [ "$1" = "gunicorn" ]; then
    echo "[entrypoint] Applying database migrations..."
    python manage.py migrate --noinput

    echo "[entrypoint] Collecting static files..."
    python manage.py collectstatic --noinput

    echo "[entrypoint] Starting gunicorn..."
fi

exec "$@"
