# Meso — athlete-facing slice plan

**Status:** Phase 1 in progress · created 2026-06-27
**Companion to:** [`decisions.md`](./decisions.md) (B2, S3, S7, N1/D-a/D-b) ·
[`persistence-plan.md`](./persistence-plan.md) · [`agent-plan.md`](./agent-plan.md)
**Goal of this slice:** give the **athlete** a real, logged-in surface — see the
plan their coach delivered, log the sessions they train — and feed those logs
back into the coach's results screen and the agent's grounding. This is item 3
in the [decisions.md suggested sequence](./decisions.md#suggested-sequence):
*"Athlete delivery + logging — the athlete PWA surface, notifications, then
results feeding back to the agent."*

The coach side (persistence slice) and the agent (agent slice) are complete; the
logging models (`SessionLog`/`LoggedSet`) and the delivery snapshot
(`WeekDelivery`) already exist, and the agent already grounds on
`serialize_recent_logs`. **There is no athlete-facing data yet** — every
`SessionLog` is fabricated in tests. This slice produces the first real ones.

### Decisions this rests on (see `decisions.md`)
- **B2** — athletes are `User`s who log in; the surface is responsive web / an
  installable PWA (native deferred). The coach can edit their plan; the athlete
  gets **read + logging only**.
- **N1 / D-a / D-b** — an athlete may have several coaches; plans are owned per
  `CoachAthlete` relationship; contraindications are global to the athlete.
  `Plan.objects.for_athlete(user)` already scopes to all the athlete's *active*
  coaches.
- **S3** — delivery & notifications (email via `django-ses` + `notifications`;
  push needs the PWA). **S7** — offline logging (gym wifi is bad) wants a PWA.
  Both land in the PWA phase.

### What the athlete may see (the scoping contract)
- Plans via `Plan.objects.for_athlete(user)` — i.e. across every **active**
  coach link. A declined/ended/pending link shows nothing.
- Within a plan, only **delivered** weeks (`Week.delivered_at` is set). A coach's
  in-progress, undelivered week is invisible — delivery is the publish gate.
- Logging writes only the athlete's own `SessionLog`/`LoggedSet` rows; an athlete
  can never read or write another athlete's logs, nor any coach surface.

---

## Architecture

The athlete surface lives at its own URL prefix, **`/meso/me/`**, distinct from
the coach's view of an athlete (`/meso/athlete/<uuid>/`). It is server-rendered
(htmx + Alpine, matching the rest of Meso); the logging interaction reuses the
prototype's "phone" styling already inline in `designer.html`. The write path is
a small JSON endpoint, mirroring the designer's autosave seam (no DRF).

```
/meso/me/                     AthleteHomeView   — the athlete's active plans, each
                                                  with its latest delivered week +
                                                  that week's sessions (done/pending)
/meso/me/session/<id>/        AthleteSessionView — one delivered session, its
                                                  prescribed exercises (Phase 1 read-only;
                                                  Phase 2 logs sets)
/meso/api/me/session/<id>/log/  athlete log endpoint (Phase 2)
```

Scoping helpers (athlete-side analogues of `_coach_plan_or_forbidden`):

- `Plan.objects.for_athlete(user)` (exists) — the athlete's active-coach plans.
- `_athlete_session_or_404(request, pk)` (new) — a `Session` whose
  `week.mesocycle.plan` is in `for_athlete(user)` **and** whose week is
  delivered; anything else (foreign athlete, undelivered week, unknown id) is a
  flat 404. Never a silent empty render.

Serializers/presenters reuse the coach-side ones where the shape matches
(`serialize_session` already emits the prescribed grid) and add athlete-only
read helpers (delivered weeks, per-session log status) rather than leaking coach
fields (volume/intensity bars, agent adjustments) to the athlete.

### Role-aware navigation
`_meso_base.html`'s top nav is coach-only today (Roster / Designer, a hardcoded
"LG" coach avatar). Phase 1 wraps the nav links + avatar in overridable blocks so
the athlete templates supply their own ("Training" + the athlete's initials)
while **coach screens render byte-identical** (the default block content is the
existing markup). A *pure* athlete (no `CoachProfile`, but an active coach link)
who lands on the coach roster is redirected to `/meso/me/`; a coach — or a
coach-who-is-also-an-athlete — keeps the roster.

---

## Phasing (one PR each)

**Phase 1 — Athlete home + session (the read surface). ⏳ In progress.**
The athlete's own logged-in surface, read-only: `/meso/me/` lists their active
plans with each plan's latest delivered week and its sessions (each marked
done/pending from the athlete's `SessionLog`); `/meso/me/session/<id>/` shows one
delivered session's prescribed exercises. Athlete-scoped serializers/presenters,
role-aware nav, the roster→home redirect for pure athletes. **No logging writes,
no migration** (the logging models already exist).
*Done when:* an athlete logs in, sees only the weeks their coach delivered (never
another athlete's, never an undelivered week), and opens a session to read its
prescription. The write path + results-feedback are later phases.

**Phase 2 — Session logging (the write path).**
The session screen becomes the interactive logger (the phone-style set rows):
`POST /meso/api/me/session/<id>/log/` upserts the athlete's `SessionLog` and its
`LoggedSet` rows (reps/load/rpe per set), flips the session done, and stamps the
date. Athlete-scoped (only the logged-in athlete's own logs; only delivered
sessions). This produces the first real rows `serialize_recent_logs` grounds the
agent on. *Done when:* an athlete logs a session and it survives reload — and the
agent's grounding sees it.

**Phase 3 — Results feed back (close the loop).**
Retire `mockdata.RESULTS_*`: the coach's results screen reads real
`SessionLog`/`LoggedSet` against the prescribed targets (completion, RPE vs
target, flags), and the designer's `last`/`adj` fields light up from logs. The
agent already consumes `recent_logs`; this surfaces the same truth to the coach.
*Done when:* a logged session drives a real results screen and the designer's
"last time" column, with no fixtures left on the coach side.

**Phase 4 — PWA + delivery notifications (S3 / S7).**
A web-app manifest + service worker (installable, offline-tolerant logging), and
delivery notifications (email via `django-ses` + the `notifications` app; web
push deferred). The coach's "Deliver to her app" becomes literally true.
*Done when:* the athlete can install Meso, log with flaky wifi, and gets notified
when a week is delivered.

## Out of scope (later)
Groups (S1, shared program + per-athlete override) · cross-coach scheduling
collisions in the athlete app · native apps · the full "changes since last
delivery" diff UI (the `WeekDelivery` snapshot is captured; the diff renders
here or with the coach review surface).

## Testing
pytest + factory_boy. Priorities, mirroring the coach-side discipline:
**scoping** (athlete A cannot see athlete B's sessions; a pending/ended coach
link shows nothing; an undelivered week is invisible), **access control**
(non-owned session → 404, login required), and (Phase 2) **log ownership**
(an athlete writes only their own `SessionLog`).
