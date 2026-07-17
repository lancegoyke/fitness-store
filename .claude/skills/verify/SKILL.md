---
name: verify
description: Build, launch, and drive the Django app locally to verify a change end-to-end over HTTP (no Docker needed).
---

# Verifying changes in fitness-store

The surface is the Django server. Docker (`just services` → Postgres 5434 /
Redis 6334) may be unavailable in sandboxed sessions — a file-backed SQLite
override works for everything except `select_for_update` semantics.

## Launch (no Docker)

1. Write a throwaway settings module in the scratchpad:

   ```python
   # <scratch>/verify_settings.py
   from config.settings.test import *  # noqa
   DEBUG = False              # DEBUG=True pulls in debug_toolbar → crash
   ENVIRONMENT = "TESTING"    # "DEVELOPMENT" makes urls.py import debug_toolbar
   ALLOWED_HOSTS = ["*"]
   DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                            "NAME": "<scratch>/verify.sqlite3"}}
   EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
   ```

2. From `app/`: `PYTHONPATH=<scratch> DJANGO_SETTINGS_MODULE=verify_settings
   uv run python manage.py migrate` then `... runserver 8035 --noreload`
   (background). Emails print to the server log (console backend).

3. Seed via `manage.py shell < seed.py` — create users with
   `create_user(..., password=...)` so form login works; meso needs
   `CoachProfile`, `CoachAthlete` (ACTIVE), `Plan` → `Mesocycle` →
   `SessionSlot`/`ExerciseSlot` (block-level identity) → per-week `Week`,
   `Session`, `Prescription(line=0, text=...)`.

## Drive

- Login is allauth email+password: GET `/accounts/login/` into a curl cookie
  jar, then POST `csrfmiddlewaretoken` (from the jar) + `login` + `password`
  with a `Referer` header. 302 → `/users/profile/` = success.
- Athlete surface: `/meso/me/` (home), `/meso/me/session/<pk>/` (logger page),
  POST `/meso/api/me/session/<pk>/log/` (JSON `{"sets": []}`, needs
  `X-CSRFToken` + `Referer`).
- Coach surface: `/meso/deliver/<plan_id>/` (deliver screen), POST
  `/meso/api/plan/<plan_id>/deliver/` (empty body = current week's block).
- Designer JS: `static/js/dist/designer.js` is NOT committed — `npm run build`
  if you need the designer page to render its island.

## Gotchas

- Admin lives at `/backside/`; `/admin/` is a honeypot.
- SQLite hides `select_for_update(skip_locked=…)` behavior — anything
  concurrency-shaped needs the real Postgres services.
