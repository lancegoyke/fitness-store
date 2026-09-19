alias django := dev

# Development commands (unique port to avoid conflicts)
dev:
    uv run python app/manage.py runserver 8034

# Docker development
docker-dev:
    docker compose up -d --build

# Run the django-q2 cluster (executes scheduled tasks, e.g. the invite sweeps)
qcluster:
    uv run python app/manage.py qcluster

# Django management commands
manage *args:
    uv run python app/manage.py {{ args }}

# Database operations
migrate:
    uv run python app/manage.py migrate

makemigrations:
    uv run python app/manage.py makemigrations

seed-database:
    uv run python app/manage.py seed_database

createsuperuser:
    uv run python app/manage.py createsuperuser

# Testing and code quality
test:
    uv run pytest

test-coverage:
    uv run coverage run -m pytest
    uv run coverage report -m

# Front-end unit tests for the meso JS (vitest). Run `npm install` once first.
test-js:
    npm test

# Rebuild the designer React island (frontend/designer/) on every save. Run
# this alongside `just dev` if you're touching the designer page — there's no
# HMR yet (Decision 3), and `just dev` alone won't build it: the page will
# 404 on dist/designer.js in DEBUG until this (or `frontend-build`) has run.
frontend-watch:
    npm run watch

# One-shot production build of the designer island to
# app/store_project/static/js/dist/ (also runs in the Dockerfile node stage
# and in Frontend CI to catch a broken build before it ships).
frontend-build:
    npm run build

# Local, opt-in headless-browser E2E suite for the Meso UI (issue #506):
# phone + desktop journeys driven by Playwright against a real in-process
# Django server (pytest-django's `live_server`). NOT part of `just test`/CI
# — it's slower and meant to be run (and watched) while you're actually
# changing the Meso UI. Builds the designer bundle first since
# `static/js/dist/` is gitignored and a missing bundle mounts an empty div
# with no error. One-time setup: `uv run playwright install chromium`.
# Examples: `just e2e --headed` opens a real browser window; `just e2e -k
# phone` runs only the phone-viewport half; `PWDEBUG=1 just e2e -k login`
# opens the Playwright Inspector.
e2e *args:
    if [ ! -d node_modules ]; then npm install; fi
    npm run build
    uv run pytest e2e -m e2e --tracing=retain-on-failure --output=e2e/test-results {{ args }}

lint:
    uv run ruff check

format:
    uv run ruff format

format-unsafe:
    uv run ruff check --fix --unsafe-fixes

pre-commit:
    pre-commit run --all-files

check: pre-commit lint test

# Deploy to Hetzner: push to GitHub, then build + migrate + restart on the box.
deploy:
    git push origin main
    deploy deploy -a fitness-store

# Tail production logs (optionally a single service, e.g. `just prod-logs web`)
prod-logs *args:
    deploy logs -a fitness-store {{ args }}

# Run an on-demand production database backup (pg_dump -Fc to /srv/.../backups)
prod-backup:
    deploy backup run -a fitness-store

# Show production status (containers, ports, TLS)
prod-status:
    deploy status -a fitness-store

# Utility commands
shell:
    uv run python app/manage.py shell

# Run shell command with -c option
shell-c command:
    uv run python app/manage.py shell -c "{{ command }}"

collectstatic:
    uv run python app/manage.py collectstatic

# Start services (PostgreSQL on 5434, Redis on 6334)
services:
    docker compose up -d

stop-services:
    docker compose down

# Database shell (connects to local PostgreSQL)
db-shell:
    docker exec -it fitness_store_postgres psql -U postgres

# Regenerate the Meso walkthrough video (docs/demo/README.md) — seeds demo
# data, drives the real coach + athlete flow in headless Chromium, and writes
# docs/demo/out/meso-walkthrough.mp4. Zero manual steps; safe to re-run.
# `--wait` blocks on the DB/Redis healthchecks (a plain `up -d` returns before
# Postgres accepts connections, and a cold run's first `migrate` would fail).
record-demo:
    docker compose up -d --wait
    uv run python scripts/record_demo.py

# Regenerate the Meso landing-page hero screenshot (issue #415) — reuses
# record_demo.py's server/seed plumbing to drive a real coach into the
# Designer, then saves a WebP still instead of a video. Writes
# app/store_project/static/webp/meso-landing-designer.webp. After a Designer
# UI change, re-run this and `git add` the new WebP — that's the whole
# refresh story, nothing else to update. See also `capture-landing-cards`,
# the per-card sibling of this recipe.
capture-landing-still:
    docker compose up -d --wait
    just frontend-build
    uv run python scripts/capture_landing_still.py

# Regenerate the Meso landing page's three "how it works" card stills
# (Design/Deliver/Adapt — issue #415 follow-up), the small sibling of
# `capture-landing-still`'s hero shot. Drives a real coach into the Designer
# + review screen and a real athlete into her session view (phone viewport),
# writing app/store_project/static/webp/meso-card-{design,deliver,adapt}.webp.
# After a UI change to the Designer, athlete session view, or review screen,
# re-run this and `git add` the new WebPs.
capture-landing-cards:
    docker compose up -d --wait
    just frontend-build
    uv run python scripts/capture_landing_still.py --cards

# Publish the walkthrough video (docs/demo/out/meso-walkthrough.mp4) + a poster
# frame to S3 (issue #415 follow-up) so the Meso landing page has something to
# embed. Needs a video to already exist — this doesn't record one. No docker
# services required (it's just an upload). The full refresh loop, after any UI
# change, is:
#
#     just record-demo && just publish-demo-video
publish-demo-video:
    uv run python scripts/publish_demo_video.py
