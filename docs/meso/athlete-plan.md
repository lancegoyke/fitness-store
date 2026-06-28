# Meso — athlete-facing slice plan

**Status:** Phase 1 done & merged (PR #288, squash `42bb805`; deployed to
Hetzner — no migration). Phase 2 (session logging) done & merged (PR #290, squash
`31d2913`; no migration). **Phase 3 (results feed back) done & merged** (PR #291,
squash `b8f0966`; 2026-06-28; Django CI green, deployed to Hetzner — no migration;
+31 tests, 302 meso / 441 project-wide; ruff clean; `mockdata.py` deleted — every
coach-side screen is DB-backed now; local Codex review 0 blocking across 3 rounds
→ CLEAN, 4 nits fixed) · created 2026-06-27 · **Phase 4 split into 4a (delivery
notifications — done & merged, PR #293, squash `dfbebee`; deployed to Hetzner;
+9 tests, no migration) + 4b (PWA + web push — **built**, see Phase 4b below;
+43 tests, 354 meso / 494 project-wide; ruff clean; **one migration**
`0006_pushsubscription`; new dep `pywebpush`).**
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
  in-progress, undelivered week is invisible — delivery is the *visibility* gate.
- Logging writes only the athlete's own `SessionLog`/`LoggedSet` rows; an athlete
  can never read or write another athlete's logs, nor any coach surface.

**Design note — live contents vs. the delivery snapshot.** Delivery gates whether
a week is *visible* to the athlete, not whether its contents are *frozen*. Once a
week is delivered, the athlete sees its **current (live)** sessions/prescriptions,
so a coach correcting an already-delivered week is reflected immediately — the
intended behaviour for a fix-in-place. The frozen `WeekDelivery.payload` snapshot
is the historical record that powers the (deferred) "changes since last delivery"
diff, *not* a second athlete-facing render. This deliberately avoids a live/snapshot
split that would also fight Phase 2 logging (which targets live prescription rows).
A stricter "re-deliver to publish edits" gate (e.g. flag a delivered week dirty on
edit) is a later-slice option if coaches want edits staged rather than live.

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

**Phase 1 — Athlete home + session (the read surface). ✅ Done & merged (2026-06-27, PR #288, squash `42bb805`; Django CI green, deployed to Hetzner — no migration; `/meso/me/` live, login-gated).**
The athlete's own logged-in surface, read-only: `/meso/me/` lists their active
plans with each plan's latest delivered week and its sessions (each marked
done/pending from the athlete's `SessionLog`); `/meso/me/session/<id>/` shows one
delivered session's prescribed exercises. Athlete-scoped serializers/presenters,
role-aware nav, the roster→home redirect for pure athletes. **No logging writes,
no migration** (the logging models already exist).
*Done when:* an athlete logs in, sees only the weeks their coach delivered (never
another athlete's, never an undelivered week), and opens a session to read its
prescription. The write path + results-feedback are later phases.

*Built* (branch `meso-athlete-phase1`): `AthleteHomeView` (`/meso/me/`) and
`AthleteSessionView` (`/meso/me/session/<id>/`), both `LoginRequired`, with the
athlete-side scoping helpers `_athlete_plans` (active-coach + non-archived) and
`_athlete_session_or_404` (delivered week + owned plan, else a flat 404).
`serializers.latest_delivered_week` + `presenters.athlete_home`/`athlete_session`
format the read shape (reusing `serialize_prescription`); log status reads only
the athlete's own `SessionLog`. `_meso_base.html` gains overridable `navlinks` +
`topnav_avatar` blocks so the athlete templates (`athlete_home.html`,
`athlete_session.html`) carry their own nav while coach screens render
identically; `RosterView` redirects a pure athlete (active link, no
`CoachProfile`) to `/meso/me/`. Built red→green: +20 tests
(`test_athlete_surface.py` — scoping, delivered-only, non-owner/undelivered/
archived 404s, login guards, log-status, roster redirect); 239 meso / 379
project-wide pass, no migration. **Local Codex review clean (1 round):** it
flagged that "delivery is the publish gate" over-claimed (live rows are rendered,
so post-delivery coach edits show through) — resolved by making the contract
precise (delivery gates *visibility*; contents are live; the `WeekDelivery`
snapshot is the deferred-diff record) rather than splitting live vs. snapshot,
which would fight Phase 2 logging. See the **Design note** above. **Deferred:**
the logging write path (Phase 2), results-feedback (Phase 3), PWA + notifications
(Phase 4).

**Phase 2 — Session logging (the write path). ✅ Built (2026-06-27, branch `meso-athlete-phase2`; no migration).**
The session screen becomes the interactive logger (the phone-style set rows):
`POST /meso/api/me/session/<id>/log/` upserts the athlete's `SessionLog` and its
`LoggedSet` rows (reps/load/rpe per set), flips the session done, and stamps the
date. Athlete-scoped (only the logged-in athlete's own logs; only delivered
sessions). This produces the first real rows `serialize_recent_logs` grounds the
agent on. *Done when:* an athlete logs a session and it survives reload — and the
agent's grounding sees it.

*Built:* `athlete_log_session` (`POST /meso/api/me/session/<id>/log/`), scoped by
the same `_athlete_session_or_404` as the read surface (foreign / undelivered /
archived / unknown session → flat 404). The body is **validated before any
write** (`_clean_logged_sets`) — bad input is a 400 that persists nothing — and
the write is wrapped in a transaction: it upserts the athlete's own `SessionLog`
(most-recent-wins, so re-logging updates the one log) and **replaces** its
`LoggedSet` rows. `serialize_session_log` echoes the saved state back.
`presenters.athlete_session` now formats the prescribed grid into set-input rows
pre-filled from the athlete's existing log (carrying the coach's full target —
sets×reps · load · RPE); `athlete_log_payload` trims that to the JSON the
`athlete_session.html` + `meso_athlete.js` Alpine logger hydrates from and POSTs
("Save progress" / "Log session" hit the same idempotent endpoint, differing
only in the status they stamp). Built red→green: **+33 tests**
(`test_athlete_logging.py` — access control, write semantics, validation,
ownership isolation, survives-reload, agent grounding); 272 meso / 412
project-wide pass, ruff clean, **no migration** (`SessionLog`/`LoggedSet` already
exist). **Local Codex review: 0 blocking across 5 rounds.** Nits fixed: bound
`set_number` (1–50) + hard-cap the render so a wild value can't balloon the page;
reject duplicate `(prescription, set_number)` keys; show the prescribed load/RPE;
"Save progress" preserves a done status (no downgrade); sync row check-state from
the saved log; and **preserve the workout date on later edits** (a missing date
defaults to today only on create — re-saving keeps the original date, so a later
fix doesn't reorder grounding). **Declined by design:** a `unique(session,
athlete)` constraint Codex raised twice — the model intentionally allows multiple
logs per athlete/session (`serialize_recent_logs` + the capped-newest-first
grounding test treat them as dated history), so the constraint would break
grounding; the sequential re-save path stays idempotent and a concurrent
double-submit at worst adds one history row, not corruption. **Deferred:**
results-feedback (Phase 3), PWA + notifications (Phase 4).

**Phase 3 — Results feed back (close the loop). ✅ Done & merged (2026-06-28, PR #291, squash `b8f0966`; Django CI green, deployed to Hetzner — no migration).**
Retire `mockdata.RESULTS_*`: the coach's results screen reads real
`SessionLog`/`LoggedSet` against the prescribed targets (completion, RPE vs
target, flags), and the designer's `last` field lights up from logs. The
agent already consumes `recent_logs`; this surfaces the same truth to the coach.
*Done when:* a logged session drives a real results screen and the designer's
"last time" column, with no fixtures left on the coach side.

*Built:* the designer's **"last time" column** — `serializers.last_logged_labels`
maps each rendered prescription to a compact summary of the athlete's most recent
logged sets for that lift (matched by exercise identity — catalog FK, else
case-folded name — so a prior week's "Box Squat" surfaces against the current
week's), threaded into `serialize_plan` as a per-exercise `last` (e.g.
`4×6 · 70kg · RPE8.5`); one query over the plan's logged sets, absent when the
lift was never logged (the no-log round-trip still holds). The **results screen**
is now session-bound (`results/<session_id>/`, coach-scoped via
`_coach_session_or_404`; the bare `results/` redirects to the coach's
most-recently-logged session, else the roster — mirroring designer/deliver).
`presenters.session_results` scores the athlete's most recent `SessionLog`
against the prescribed grid: per-exercise target/logged/RPE rows (reps grouped,
load suffixed, the hardest set's RPE; `rpe_state` lights "over" on any overshoot),
plus completion %, mean RPE-vs-target, and the **flags** worth acting on (an RPE
overshoot ≥ 1.0); an unlogged session renders an honest awaiting state, not
invented numbers. **`mockdata.py` is deleted** — its `RESULTS_*` were its last
users, so every coach-side screen is DB-backed now. `seed_meso_demo` delivers +
logs Maya's current-week "Lower" session so the deployed demo's results screen
and designer `last` column light up off real rows. Built red→green: **+31 tests**
(`test_results.py` — scoping, bare redirect, the metrics, pending-draft/awaiting
states, rep-shortfall, free-form completion; `test_serializers.py::TestLastLoggedColumn`;
`test_seed_demo.py` — the demo log drives results + the `last` column). 302 meso /
441 project-wide pass, ruff clean, **no migration**. **Local Codex review: 0
blocking across 3 rounds → CLEAN.** Four nits fixed: a pending "Save progress"
draft is excluded from both the results screen and the `last` column (a partial
session isn't completed feedback); completion falls back to the logged count for
a free-form set cell ("AMRAP") so it can't divide by zero; and results notes catch
a **rep** shortfall (a 3×12 logged as 12,12,9), not just a missing-set one. None
declined. **Deferred:** the group-only `adj` overlay rides with groups (S1, out of
scope); PWA + notifications (Phase 4).

**Phase 4 — PWA + delivery notifications (S3 / S7).** Split into two PRs — the
notification half (4a) ships independently of the PWA half (4b), since email is
backend-testable today while the service worker/offline queue is browser-side.

**Phase 4a — Delivery notifications (S3). ✅ Done & merged (2026-06-28, PR #293, squash `dfbebee`; Django CI green, deployed to Hetzner — no migration).**
When a coach delivers a week (`POST api/plan/<id>/deliver/`), the athlete is
emailed that their next training week is ready, with an absolute link to their
own surface (`/meso/me/`). Channel is the one that exists today — `send_mail`
via `django-ses`, templated through the `notifications` app (plain + HTML).
`notifications.emails.send_week_delivered_email` is pure/testable (takes the
resolved athlete/coach/plan/week + an absolute `home_url`; returns `False` and
sends nothing when the athlete has no address); the view's
`_notify_athlete_delivered` builds the URL from the request and calls it
**best-effort** — delivery has already committed, so a mail failure is swallowed
and logged, never a 500 or a rolled-back deliver. Only fires on a *successful*
deliver (the 403/404/400 guards return before the email) and only reaches the
athlete (never the coach). Built red→green: **+9 tests**
(`test_delivery_notifications.py` — emails the athlete once, names coach/plan/
week + links home, skips a no-address athlete, survives a backend failure,
athlete-only, re-deliver notifies again, forbidden/unauthenticated send nothing);
311 meso / project-wide pass, ruff clean, **no migration**. *Done when:* a coach
delivers and the athlete gets a "your week is ready" email linking to `/meso/me/`.
**Deferred:** debouncing rapid re-delivers (each explicit deliver notifies);
async send off the request thread (synchronous best-effort matches the existing
`payments`/`notifications` pattern); web push (rides with the PWA, 4b).

**Phase 4b — PWA + web push (S7 / S3). ✅ Built (2026-06-28, branch `meso-athlete-phase4b`; one migration `0006_pushsubscription`).**
The athlete surface becomes installable, offline-tolerant, **and push-capable**:
a web-app manifest + service worker so Meso installs and opens through flaky gym
wifi, plus web push so a coach's deliver reaches the athlete's lock screen (the
push half S3 deferred from 4a — "web push rides with the PWA"). *Done when:* the
athlete installs Meso, logs offline (syncing when wifi returns), and gets a push
when their coach delivers.

*Built (installable PWA, S7):* the manifest (`/meso/manifest.webmanifest`) and
service worker (`/meso/sw.js`) are served as **views, not static files** —
WhiteNoise's `CompressedManifestStaticFilesStorage` hashes static filenames, so
a worker shipped as a static file would have an unstable URL and the wrong scope;
a worker only controls pages at/below its own path, so it lives at `/meso/sw.js`
to control `/meso/me/`. The worker is rendered from a template (`meso/sw.js`)
that resolves the **hashed** precache URLs via `{% static %}` at render time, so
the cached shell auto-busts every deploy (verified: under production `DEBUG=False`
+ manifest storage the precache lists `meso_push.<hash>.js` etc.; the worker
itself is served `Cache-Control: no-cache`). Strategy: precache the static shell
+ offline page on install; **network-first** for navigations, falling back to the
last-good cached page then the offline page (so a session opened online keeps
working when wifi drops mid-set); stale-while-revalidate for static GETs; POSTs
(logging) pass through untouched — the page's own offline queue owns writes
(more reliable on iOS than Background Sync). `meso/offline.html` is the
login-free fallback the worker caches. The PWA chrome (manifest link, theme/apple
meta, apple-touch icon, SW registration) rides an athlete-only `pwa` block on
`_meso_base.html` via the `_pwa_head.html` include, so **coach screens stay plain
web** (no manifest, no worker) — mirroring Phase 1's athlete-only nav blocks.
Generated square install icons (192/512 + maskable + apple-touch) from the brand
mark. *Note:* the offline **logging queue** itself (stash a failed save, flush on
reconnect) lives in `meso_athlete.js`'s save path / is browser-side; the
server-side guarantee that makes it safe — replaying the same log POST is
idempotent — is pinned by a test.

*Built (web push, S3):* a `PushSubscription` model (athlete FK, unique
`endpoint`, `p256dh`/`auth` keys; the only migration this slice) the browser
registers via `POST api/me/push/subscribe/` (login-scoped to the caller, upsert
by endpoint so a device reassigns to whoever's logged in; `unsubscribe/` drops
only your own). `meso/push.py` signs + sends with VAPID (`pywebpush`):
`notify_week_delivered` pushes the delivery payload to each of the athlete's
devices, **prunes a dead endpoint** (404/410 Gone), swallows+logs any other
per-device failure, and is a **silent no-op when VAPID keys aren't configured**
(`settings.MESO_VAPID_*`) — the same graceful degradation as the no-address
email, so the app boots and CI runs without creds. The deliver hook
(`_notify_athlete_delivered`) now fires email **and** push, each independently
best-effort on `transaction.on_commit` — a push failure never rolls back or 500s
the deliver, and only the athlete is pushed (never the coach). The service
worker's `push`/`notificationclick` handlers render the notification and focus/
open `/meso/me/`; `meso_push.js` runs the gesture-driven subscribe flow (an
"Enable notifications" CTA on the home, shown only when push is usable + the user
hasn't decided), reading the VAPID key + CSRF from the `_pwa_head.html` config.

Built red→green: **+43 tests** — `test_athlete_pwa.py` (manifest install fields,
worker content-type + `Service-Worker-Allowed` scope + precache, login-free
offline page, athlete templates wire manifest/worker while the coach roster does
not, offline-replay idempotency) and `test_push.py` (subscribe/unsubscribe access
control + validation + upsert + ownership, the signer's VAPID args + disabled
no-op, `notify_week_delivered` payload/pruning/scoping, deliver-triggers-push
best-effort + athlete-only, push config in the page). 354 meso / 494 project-wide
pass, ruff clean. **Deployment verified:** `collectstatic` succeeds, the worker
view renders hashed precache URLs under manifest storage, `makemigrations --check`
clean, `check --deploy` unchanged. **Deferred:** Background Sync (the page queue
covers offline logging cross-browser incl. iOS); debouncing rapid re-deliver
pushes; a richer in-app notification-settings surface.

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
