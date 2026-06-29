# Meso — app-managed scheduling (django-q2)

## Why

The N4 invite lifecycle needs two recurring sweeps — `meso_expire_invites`
(age out past-due claim links) and `meso_remind_expiring_invites` (email a
reminder before a link lapses). Phase 4 shipped them as cron-ready management
commands but with *no scheduler*: they only ran if someone invoked them.

Hand-rolling a cron on the Hetzner box would work but isn't versioned, doesn't
deploy with the code, and dies on a box rebuild. So scheduling is **managed by
the app** instead.

## Decision

**django-q2 with the Django ORM as the broker.**

- **ORM broker, not Redis.** The shared box's Redis is small and `noeviction`
  (it backs cache + sessions); a task backlog there could starve logins. Task
  volume is tiny (two daily sweeps), so the Postgres-backed broker is more than
  enough and keeps Redis pressure off. (`Q_CLUSTER["orm"] = "default"`.)
- **Lightest "real" option.** One extra worker process (`qcluster`) versus
  Celery's worker **+** beat. Schedules are DB rows editable in the Django admin.
- **Doubles as the queue the agent wants.** `meso/agent/jobs.py` runs proposal
  work on a bare daemon thread today and its own comments ask for "a real worker
  queue later." Migrating that onto `async_task` is a clean follow-up (see
  Deferred) now that a cluster exists.

Considered and rejected: **Celery + beat** (heavier — two processes + a broker —
for two daily sweeps on a memory-tight box); a **bespoke loop container /
APScheduler** (doesn't generalize, no admin visibility); **box cron** (the
un-versioned approach we're moving away from).

## Shape

- **`Q_CLUSTER`** (`config/settings/base.py`): ORM broker, 1 worker,
  `catch_up=False` (no burst of missed runs after downtime), `retry > timeout`.
  Tests override with `sync=True` so no cluster ever spins up.
- **`store_project/meso/tasks.py`**: stable wrappers (`expire_invites`,
  `remind_expiring_invites`) that each call their management command — one home
  for the sweep logic, a stable dotted path for the schedule to point at.
- **Migration `meso/0018_register_invite_schedules`**: idempotent data migration
  that registers the two `django_q.Schedule` rows (DAILY), keyed on name and
  reversible, depending on `("django_q", "__latest__")`. Versioned + deploys with
  the code; a coach can still pause/retime in admin afterward.
- **`qcluster` service** (`docker-compose.production.yml`): same image as `web`,
  run as `python manage.py qcluster`; waits on `web` being healthy so migrations
  are applied first; 256 MB limit. Locally: `just qcluster`.

## Operating notes

- The box's committed memory grows by the qcluster limit (256 MB). Confirm
  headroom; tune `workers` / the limit if needed.
- The entrypoint only migrates/collectstatic for the `gunicorn` command, so the
  `qcluster` command passes straight through without re-running them.

## Deferred

- **Migrate `meso/agent/jobs.py`** off its daemon thread onto `async_task` (the
  unit of work, `run_proposal_job`, already exists — it's a drop-in dispatch
  swap). Bigger + changes the agent endpoint's async behavior; its own PR.
- **Configurable schedule times / per-coach cadence**; an admin surface beyond
  the raw `Schedule` rows.
- **Result monitoring / alerting** on a failed sweep (today: cluster logs + the
  admin's task list).
