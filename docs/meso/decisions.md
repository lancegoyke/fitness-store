# Meso — decisions to make before building real

**Status:** living document · started 2026-06-26
**Context:** The Meso program designer + coaching-loop screens exist today as a
**fully client-side mock** (PRs #267, #268). There is no database, no real agent,
no athlete app — every screen reads canned fixtures from `app/store_project/meso/mockdata.py`.
This doc tracks the decisions that gate turning those mocks into real code, so we
don't write schema or architecture on top of unsettled product questions.

How to read the status field:
- **⏳ Awaiting decision** — genuinely your call; blocks real work until answered.
- **🟡 Proposed** — architect's recommendation; will proceed on the rec unless overridden.
- **✅ Decided** — locked, with the choice recorded.

---

## Blockers (foundation — schema & architecture depend on these)

### B1 · Product shape & tenancy — single-coach or multi-coach?
Is Meso *you* programming for your own clients (single-tenant), or a tool other
coaches log into (multi-tenant SaaS)? Decides whether "coach" is a model, whether
every query is tenant-scoped, and the entire permissions layer. The store today is
B2C (sells programs to individuals); Meso is a different B2B-ish shape.

- Options: (a) single-coach, you only; (b) multi-coach SaaS now; (c) single-coach now but keep a `coach` FK so multi-coach is a clean retrofit.
- **Rec:** (c).
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **(b) Multi-coach SaaS from day one.** Tenant-scope everything: coaches are
  accounts, data is isolated per coach, athletes belong to a coach via a relationship. This
  promotes a tenancy + roles + permissions spine into even the first (persistence) slice —
  see N1–N4 below.

### B2 · What is an "athlete," and do they log in?
(a) Reuse the existing `User` model (UUID PKs, allauth) for athletes, or a lightweight
`Client` record with no login? (b) Do athletes get an **app** at all, or does the coach
log everything? The phone view, "Deliver to her app," and push notifications all imply an
authenticated athlete surface. (c) If yes — native, PWA, or responsive web?

- **Rec:** athletes = `User`s; athlete surface = responsive web/PWA first (no native).
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **Athletes are `User`s who log in** (responsive web / installable PWA; native
  deferred) **and** their coach can edit their program. So we need a coach↔athlete relationship
  that grants the coach edit rights on the athlete's plan (see N1). A single `User` may act as
  both coach and athlete.

### B3 · Domain schema & relationship to the existing `Program` product.
The store already has a sellable `Program` product; a Meso plan is a different thing
(personalized, periodized, mutable). Decide: distinct entities, and the granularity —
`Plan → Mesocycle → Week → Session/Day → ExercisePrescription → SetPrescription`, plus
`SessionLog → LoggedSet`. And: do we need **draft vs delivered versions** + a
`ProposedChange` diff record? (The review screen and "changes since last delivery" only
exist if programs are versioned.)

- **Rec:** distinct from store `Program`; full hierarchy; yes to draft/published versioning + a `ProposedChange` entity (load-bearing for the agent + review gate).
- **Status:** 🟡 Proposed (architect's call once B1/B2 land).
- **Decision:** _tbd_

### B4 · Exercise source — catalog FK, free text, or hybrid?
There's an `exercises` app with videos + alternatives. The prototype lets a coach type any
exercise name AND shows "knee-safe" tags + per-athlete swaps. Decide whether a prescription
FKs to a catalog `Exercise` (enables the picker, alternatives, contraindication matching,
agent grounding) or stays free text.

- Options: (a) hybrid — catalog when matched, free-text fallback; (b) catalog only; (c) free text only.
- **Rec:** (a) hybrid — the agent and auto-adjust need the catalog to be useful.
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **(a) Hybrid.** A prescription FKs to a catalog `Exercise` when one matches
  (nullable), with a free-text name as fallback. Picker/alternatives/contraindication-matching
  light up for catalog-linked exercises; coaches can still type a one-off.

### B5 · Front-end data flow — htmx server-rendered, or JSON API + client state?
Every interactive screen is ephemeral client state today. To persist, pick a fork: lean on
**htmx** (server-rendered partials, Django owns state — matches the rest of the app) vs. a
**JSON API (DRF) + Alpine/JS** owning richer client state (better for the spreadsheet-like
designer grid). Shapes how all five screens get wired; hard to reverse.

- **Rec:** htmx for roster/profile/review/deliver/results; a small JSON-autosave endpoint for the designer grid specifically. Don't adopt DRF wholesale unless the athlete app forces it.
- **Status:** 🟡 Proposed (architect's call; depends on B2).
- **Decision:** _tbd_

### B6 · The agent — architecture, grounding, guardrails, execution.
Provider is **Claude** (project standing guidance). Real decisions:
- **Shape:** structured tool-calling — the model emits a validated batch of program edits
  (swap / load / volume / deload) applied server-side — vs. free text. **Rec: tool-calling.**
- **Grounding:** profile, contraindications, coaching rules, recent logged sessions, exercise
  catalog; how logs get summarized into context.
- **Guardrails:** contraindications enforced in a **validation layer**, not just the prompt;
  keep **human-in-the-loop approval** (the review screen is that gate). **Rec: both.**
- **Execution:** sync / streamed ("drafting…") / background job (Redis is already in the stack).
  **Rec: background job + streamed status.**
- **Eval:** golden cases so quality doesn't silently regress.
- **Model tier + prompt caching:** pin against the `claude-api` reference at build time — not
  guessed here.
- **Status:** ✅ Decided & building (Phase 1 merged 2026-06-27, PR #280).
- **Decision:** **Tool-calling + a server-side validation layer + the human review gate.** Provider =
  Claude (`claude-opus-4-8`), forced `propose_program_changes` tool, prompt caching; adaptive thinking
  omitted (incompatible with a forced `tool_choice`). Contraindications enforced deterministically in
  `meso/agent/validation.py` (not just the prompt); the coach still approves. Execution is sync for
  now (background job + streamed status deferred); eval golden cases deferred. Full phasing in
  [`agent-plan.md`](../archive/meso/agent-plan.md).

---

## Promoted decisions (now in scope because of B1 multi-coach + B2 athlete login)

Choosing multi-coach SaaS pulls these out of "later" and into the **persistence slice** —
you can't write ownership/scoping-correct models without settling them.

### N1 · Coach↔athlete relationship & cardinality ✅
The load-bearing new model.
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **Many-to-many, athlete-consented.** An athlete may work with multiple coaches
  concurrently (and a coach has many athletes). The link is
  `CoachAthlete(coach, athlete, status, invited_by)`; relationships require the other party's
  acceptance and the **athlete can decline or end** any coach link ("if they so choose"). A
  `User` may be both coach and athlete.
- **Consequences (now load-bearing for the schema):**
  - **D-a · Plans owned per relationship** — each coach programs independently for the athlete; a
    `Plan` FKs its `CoachAthlete`. _(rec)_
  - **D-b · Athlete profile vs plan attributes** — contraindications/injuries + training history
    are **global** to the athlete (every coach sees them); goals/focus live **per plan**. _(rec)_
  - **D-c · Bidirectional invites** — coach invites athlete, or athlete requests coach; both need
    acceptance; either side can end it (archives that coach's plans, never deletes). _(rec)_
  - **Scoping** — a coach sees only athletes they have an active link to and edits only their own
    plans; an athlete sees plans from all their coaches. Cross-coach scheduling collisions in the
    athlete app are a later UX concern.

### N2 · Tenancy scoping enforcement 🟡
How isolation is guaranteed so coach A never sees coach B's athletes/plans. Manager-level
scoping by `coach`, object-level permissions, or a tenancy middleware. **Rec:** explicit
`coach` FK on tenant-owned models + a scoped base manager/queryset mixin + view-level checks.

### N3 · User roles (coach vs athlete) 🟡
The store `User` is currently a customer. Now a `User` may be coach, athlete, both, or neither.
Mark via a `CoachProfile`/`AthleteProfile`, Django groups, or boolean flags. **Rec:** a thin
`CoachProfile` (presence = is-a-coach) + the `CoachAthlete` link (presence = is-an-athlete);
avoids overloading the User model.

### N4 · Athlete onboarding / invites 🟢 (Phases 1–3 built)
How an athlete joins a coach: coach invites by email → athlete signs up (allauth) → link
created; or coach creates a stub athlete and sends a claim link. **Decision:** email invite +
claim, reusing allauth — **Phase 1 built** (the `CoachInvite` email artifact → bearer-token
claim → materialized active `CoachAthlete`; rides allauth's `?next=` with no custom adapter).
**Phase 2 built** — the reverse direction: an athlete *requests* a coach by email
(`CoachAthlete.request`), the coach accepts/declines on the roster, both sides see the pending
state on their own surface, and any non-coach now lands on their training home (where the
request form lives). **Phase 3 built** — invite *lifecycle*: a 14-day TTL (`expires_at`) + a
new `EXPIRED` status, lazy + swept expiry (the claim path refuses a stale token;
`meso_expire_invites` bulk-sweeps overdue invites), and an explicit **resend** (rotates the
token + resets the clock, re-arms an expired invite) surfaced on the roster. Plan + deferred
items in [`invites-plan.md`](../archive/meso/invites-plan.md).

---

## Secondary decisions (resolve per-slice; not blocking the foundation)

| # | Decision | Note |
|---|----------|------|
| S1 | **Groups** — "shared program + per-athlete auto-adjust" modeling (template + override diffs) | 🟡 In progress — Phase 1 (group + membership spine + read surface) + Phase 2a (shared group program + Group-mode designer) + Phase 3 (per-athlete overrides — the `adj` overlay) built; plan in [`groups-plan.md`](../archive/meso/groups-plan.md) |
| S2 | **Units & RPE vs %1RM** | ✅ Complete — units (kg/lb) shipped with earlier slices; Phase 1 (first-class `load_type` `abs`/`pct`) + Phase 2a (agent %1RM-awareness — prompt + a deterministic %1RM progression bound) + Phase 2b (athlete %1RM logging ergonomics — the estimated-1RM helper) all built & deployed. **Follow-up: persisted, coach-visible 1RM** (Phase 1 — `AthleteOneRm`, auto-derived from logged history) built. Plans in [`units-rpe-plan.md`](../archive/meso/units-rpe-plan.md) / [`one-rm-plan.md`](../archive/meso/one-rm-plan.md) |
| S3 | **Delivery & notifications** | ✅ Built — push (PWA, Phase 4b) + email (`django-ses` + `notifications`). **Email opt-out shipped** (2026-06-29): the delivery email now carries a working one-click `List-Unsubscribe` (RFC 8058) honored by a per-athlete flag — see decision log. |
| S4 | **Results ↔ `challenges`/records** | ✅ Resolved (2026-06-29, YAGNI review): **keep separate, build nothing now.** The "results screen shows a PR" premise was mock-only (the real results screen never showed a PR), and `challenges.Record` is time-based (wrong domain for a strength PR). Meso already owns best-lift data via `AthleteOneRm`. A motivational PR badge stays deferred until there's a concrete need. |
| S5 | **Real-time transport** | ❌ Deferred (YAGNI, 2026-06-29): the agent result is atomic behind a human review gate and the stack is WSGI — SSE/websockets buy ~1.5s over a cheap bounded poll for disproportionate ASGI/channels/Redis-channel-layer infra. Revisit only if the agent itself becomes genuinely streaming. |
| S6 | **Billing** | 🟢 **Planned, decisions locked** (2026-06-29): **per-active-athlete (seat) pricing** + a **free tier + 14-day no-card trial**; the paywall gates **athlete count + the AI agent** (free tier = capped seats, no agent; groups/notifications free at every tier); first slice = the **subscription spine** for existing coaches (self-serve signup later). Full plan + phasing in [`billing-plan.md`](./billing-plan.md). |
| S7 | **Offline logging** | ✅ Built — athlete PWA offline log queue (Phase 4b). |

---

## Suggested sequence (decisions unblock work, not the reverse)

Chosen first slice: **persistence first.** Note multi-coach (B1) makes this slice bigger than
plain CRUD — it carries the tenancy/roles/relationship spine (N1–N3).

0. ~~Confirm N1 cardinality~~ — **done: many-to-many, athlete-consented.**
1. **Tenancy + persistence slice** — `CoachProfile` / `CoachAthlete`, the program schema (B3),
   hybrid exercises (B4), scoped managers (N2), and designer/roster/profile reading & writing
   real data over htmx (B5). *No agent.* Replaces the most mock-y part; unblocks everything else.
2. **Agent as proposal engine** behind the existing review gate (B6). Writes `ProposedChange`s;
   coach still approves. Safe — the human gate already exists.
3. **Athlete delivery + logging** — the athlete PWA surface, notifications, then results feeding
   back to the agent. *(Built.)*
4. **Billing (S6) — the next major slice (decisions locked, plan written).** Meso is a multi-coach
   SaaS, not a single-operator tool, so coaches pay. Decided 2026-06-29: **per-active-athlete (seat)
   pricing**, a **free tier + 14-day no-card trial**, **athlete-count + the AI agent gated**, monthly/USD,
   and a **spine-first** build (existing coaches; self-serve signup later). The tenancy spine (B1) is
   already in place; what's missing is the subscription infrastructure (today's Stripe is one-time
   only). Full plan + 5-phase build in [`billing-plan.md`](./billing-plan.md). Phase 1 = the
   `CoachSubscription` model + gating accessor + local trial + comped flag (no Stripe yet). Open
   *values* (not architecture): free seat count (rec 1), per-seat price (TBD), trial 14d.

---

## E2E browser tests (#506)

`e2e/` at the repo root holds a local, opt-in browser suite for the Meso UI.
Playwright's Python sync API drives headless Chromium through pytest, against
a real Django server that pytest-django's `live_server` runs inside the test
process, on the test database. Run it while you change Meso templates or JS,
then look at the screenshots it leaves.

It is not a CI gate. No workflow runs it, and moving it into CI is a separate
decision. Chromium emulating a phone is also not iOS Safari. It can't show
Safari zooming the page when you tap an input under 16px, so a test can only
assert the computed font size.

**Setup, once.** `uv run playwright install chromium` and `npm install`.

**Running it.** `just e2e` builds the designer bundle first
(`static/js/dist/` is gitignored, and a missing bundle mounts an empty div
without an error). Then it runs every journey at three sizes: desktop
(1280×720), phone (390×844, touch, iPhone 13 user agent) and phone-360
(360×780, touch, Galaxy S8 user agent; added by #508, where layouts that only
just fit at 390 break first). Arguments pass through to pytest:
`just e2e -k phone` (both phone sizes), `just e2e -k athlete`,
`just e2e --headed` for a visible browser, `PWDEBUG=1 just e2e -k athlete` to
step through in the Playwright Inspector. Plain `uv run pytest` never collects `e2e/`
(`testpaths = ["app"]`, plus `-m "not e2e"` in `addopts`). If you call pytest
directly, pass `e2e -m e2e`.

**Output.** Screenshots go to
`e2e/screenshots/<journey>/<NN-step>--<viewport>.png`, overwritten on each
run. A failing test keeps a trace under `e2e/test-results/`. Open it with
`uv run playwright show-trace e2e/test-results/<test>/trace.zip`.
pytest-playwright empties `e2e/test-results/` at the start of every run, which
is why screenshots live in their own folder. Both are gitignored.

**Fixtures** in `e2e/conftest.py`. `login(user)` logs a browser context in by
copying the session cookie from a `force_login`. `test_login.py` is the one
test that drives the real allauth form. `press(locator)` taps on phone and
clicks on desktop. `shot(step)` saves a full-page screenshot.
`new_page(desktop=False)` opens a page in a second browser context with its
own cookies, for a journey with two people in it; `desktop=True` opens it at
1280×720 whatever the test's viewport, because the designer is desktop-only
(phones get a fallback message). `login(user, on=that_page)` and
`shot(step, on=that_page, viewport_id="desktop")` work on that page. Use
`click()`, not `press()`, on a desktop page in a phone run: it has no touch.
`delivered_plan` builds a coach, an athlete and one delivered session from the
app's factories. Every test gets a fresh browser context with service workers
blocked, so the athlete PWA's cache can't hide a server change. Wait with
`expect()` and `page.expect_response()`, never a sleep.

A journey that only makes sense at desktop (the designer, the agent) runs once
with `@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)`. The
agent journey turns on the fake agent with pytest-django's `settings` fixture
(`settings.MESO_AGENT_FAKE = True`). That changes settings for the whole
process, so the `live_server` thread sees it too, and the test settings
already run the agent inline. The test database is in-memory SQLite, and
`live_server` shares its one connection across its request threads. Two
requests at the same moment can collide there ("no such savepoint", a 500)
even though Postgres handles them fine. A journey where the page sends two
writes at once can fail for that reason alone.

**`data-testid` convention.** Kebab-case `<surface>-<thing>[-<action>]`, the
same as the React designer island: `session-log`, `sub-line-input`,
`results-logged`. Add one when a journey needs an element that has no stable
role or text. When a test checks text the user reads, find it by role or text
instead. Never style or script against a `data-testid`.

---

## First-party usage events (#509)

`analytics.Event` is one row per product moment, written only by
`analytics.track.track(name, actor=None, subject=None, **props)`. The helper
does one insert inside its own savepoint and never raises. Callers run inside
open transactions, and on PostgreSQL a failed statement would otherwise abort
the caller's transaction and roll back the write it just made
(`analytics/tests/test_track_postgres.py` pins this, and runs in the CI
Postgres job). No IP address, user agent or request path is stored.

`Event.actor` has no database constraint (`db_constraint=False`). Django's
foreign keys on PostgreSQL are checked at COMMIT, and that check locks the
referenced user row, after `track()`'s savepoint has closed. An event written
inside a delivery's transaction would lock the coach's row at commit, and could
deadlock against the agent's metering lock on the same row. `SET_NULL` still
applies on delete, from Django rather than the database.

**Excluded at the helper.** Sandbox coaches (`meso.sandbox.is_sandbox`) and
staff (`is_staff`) are dropped silently, so demo traffic and our own clicking
don't count. `TourEvent` doesn't do this: its funnel is mostly sandbox traffic
by design. A real coach's actions on demo-athlete plans (`is_demo` links) are
recorded; they're real coach actions. Since the dashboard slice, the events
about a plan (plan_created, template_imported, agent_proposal_run,
batch_applied, block_delivered) carry `props.demo`, so the dashboard can leave
them out even after the demo is removed.

**Closed set of names.** `analytics.events.EventName`. An unknown name raises
`ValueError` when `ANALYTICS_STRICT_EVENT_NAMES` is on (test settings, and
local dev with `DEBUG`) and is logged and dropped in production. Every call
site has a test that drives it, so a typo fails in the test run; in
production it costs one event, never the user's action. The model field has
no `choices`, so adding a name needs no migration.

**What counts once.**

- `set_logged` follows the `LoggedSet` rows a log gains, one event per new
  row. Typed path: a line that wasn't already showing a set of its own gets
  one (a re-blur or an edit of that set doesn't count). Structured "Log
  session"/"Save progress": that save replaces rows, so it counts the log's
  net growth. Re-posting sets, editing values, or re-posting sets the typed
  path already counted adds nothing. Removing one set and adding another in
  the same save nets zero. Where the logger deliberately keeps a row it
  can't prove the page held (after a coach reclaims a line, or skips and
  unskips a row between two edits) and writes a second one, the log really
  has two rows and the count says two.
- `session_completed` is the transition into DONE: "Log session" from a log
  that wasn't DONE (`via=log`), or the settle sweep (`via=settle`).
- `session_opened` is a page view: every GET of the session page, reloads
  included. Opens the service worker serves from cache while offline aren't
  seen.
- `push_subscribed` fires for a new endpoint or a device changing hands, not
  the re-POST `meso_push.js` makes on every page load. An endpoint the push
  service rejected (404/410) is deleted, and if the browser keeps returning
  it, the next re-POST counts again.
- `subscription_started` fires on the no-card trial (`via=trial`, at
  `CoachSubscription.start_trial_for`, which covers both entry points) and
  the first time a Stripe subscription is live (`via=stripe`, including
  trial to paid). "First time" is read from the events themselves: each
  Stripe event carries the subscription id in `props.subscription`, and a
  subscription that already has a start doesn't get another, so a
  `past_due` recovery or an out-of-order retry isn't a start. The invoice
  path (`invoice.paid` flipping `past_due` to `active`) can record the start
  too, because Stripe may deliver it before the subscription update. A
  subscription that was already live before this shipped has no start on
  record, so its first `past_due` recovery reads as one, and a cancellation
  that arrives while it's `past_due` (dunning churn) isn't recorded.
  `subscription_cancelled` fires once, when a subscription that was live
  reaches `canceled`; an `incomplete` checkout that expires never started
  and isn't a cancellation. `reason` carries Stripe's
  `cancellation_details.reason`. Two Stripe deliveries for one subscription
  processed at the same moment could each record a start; nothing
  serializes them.
- `agent_proposal_run` fires for both coach-started runs: the composer
  (`trigger=manual`) and "Draft with AI" on a new program (`trigger=draft`).
- `block_delivered` fires at `_notify_athlete_block_delivered`, once per
  delivered block (`via=deliver`, or `via=batch` per client copy).

**The client beacon.** `POST /meso/api/track/`
(`meso:track_beacon` → `analytics.views.track_beacon`) is the one door for
facts no server request reveals. The URL sits under the Meso API because the
Meso PWA is its only client; the view lives in `analytics` because the closed
name set and the exclusion rules are analytics policy, not Meso's. Body is
`{"name": …, "props": {…}}`. Guards, in order: an anonymous post gets a 204 and
records nothing (that is the beacon's answer to #542's open question about
anonymous actors — dropped, not kept); a body over 512 bytes, malformed JSON or
a non-object is a 400; more than `ANALYTICS_BEACON_PER_USER_PER_HOUR` posts from
one user in a rolling hour is a 429, cache-counted the way sandbox entry is
counted per IP; and a name or prop outside the closed set is a 400. CSRF is
Django's ordinary middleware — the beacon is not exempt. Accepted posts go
through `track()` with `source=client`, so the sandbox and staff exclusion and
the closed-name rule apply unchanged.

The accepted set is in `analytics/beacon.py`, and every accepted prop is one
short string from a closed set — no free-form values, no URLs, no user agent, so
nothing a page can compute reaches the ledger:

- `pwa_installed`, `via` ∈ {`appinstalled`, `standalone`}. Chromium fires
  `appinstalled`; iOS never does, so there the only signal is the app running
  in standalone display mode, which is true on *every* load once installed. A
  `localStorage` flag makes a device report once, whichever signal comes
  first. That flag is per browser profile, so the same person installing on a
  phone and a laptop is two installs, and clearing site data can produce a
  second one. Two rules the review added: the flag is written only once the
  beacon has **landed** (writing it first lost the install for good the first
  time an athlete opened the installed app with no signal — for a gym PWA, an
  ordinary Tuesday), and when `localStorage` can't persist at all (private
  mode, blocked site data, ITP eviction) the install is **not reported** at
  all. `isDismissed` answers "not dismissed" when storage throws, which is
  right for a card the athlete waved away and catastrophic here: on an
  installed iOS device every load is standalone, so an inert flag would report
  an install on every page view. Under-counting installs beats inventing them.
- `push_permission`, `result` ∈ {`granted`, `denied`, `default`}. Recorded only
  when the athlete actually answers: `meso_push.js` reads the permission
  *before* asking and reports only if it was `default`, and holds an in-flight
  guard so a second tap on the CTA — Chrome's prompt is a non-modal bubble, the
  page stays live under it — can't report one answer twice. `default` is itself a
  real answer — Chrome leaves the permission there when the prompt is
  dismissed.
- `push_clicked` is **not** beacon-postable, though it is one of the
  browser-only names. The server writes it, from the notification's landing
  URL (see below), with the default `source=server`: our own code observed
  that request, the way it observes `session_opened`. Accepting it at the
  beacon too was the original plan and the review killed it — nothing in the
  app posts it, so it was pure inbound surface, and it would have cost the
  thing that makes the number worth reading: today a `push_clicked` event
  always has a `PushNotification` row behind it, and any signed-in browser
  could otherwise have added events with no row.

The beacon speaks JSON and only JSON — anything else is a 415, checked before
the body is read. That isn't fussiness: `CsrfViewMiddleware` looks for
`csrfmiddlewaretoken` in `request.POST` before falling back to the
`X-CSRFToken` header, and for `multipart/form-data` that parse consumes the
stream without stashing `_body`, so a later `request.body` raises
`RawPostDataException` and the view 500s. The rate limit is checked first of
all, so it counts every authenticated attempt rather than only the well-formed
ones — otherwise malformed traffic, the kind most worth bounding, would cost
no budget. It is a fixed window anchored at the hour's first post, not a
sliding one, so a client spending its budget on either side of the boundary
gets through about twice the limit in a short span; that is accepted for a
courtesy limiter on tiny events.

**Not tracked yet.** A coach accepting an athlete's request, a relationship
re-invite and its acceptance, and the invite "Resend" button aren't events.
Designer feature use (agent panel opened, drag reorder, keyboard fill) and the
in-app toast have no beacon names yet.

## Push notification ledger (#509)

`notifications.PushNotification` is one row per push sent to one subscription —
the push peer of `SentEmail`. Before this, `meso/push.py` sent and forgot: there
was no way to answer "did the athlete get the block push" or "did anyone tap
it".

**A dedicated model, not a generic `Notification(channel=…)`.** The email half
is already two purpose-shaped tables built around SES's message id and its
event stream; a generic table would either duplicate them or force a migration
of live email rows for nothing this slice needs. Push has exactly one send and
at most one click, so one row carries the whole story, and each channel keeps
the columns its transport actually produces. `PushKind`'s values mirror
`EmailKind`'s deliberately, so the dashboard can line a push row up against the
email row for the same moment.

The primary key is a UUID because it travels in the notification's target URL,
which the athlete's browser shows and could share; a sequential id would leak
volume and let someone probe another row. `user` is `SET_NULL` like
`SentEmail.user`, so counts survive a deleted account — and unlike
`Event.actor` it keeps a real database constraint, because the row is written
from the delivery's `on_commit` callback, outside the delivery's own
transaction, so there is no commit-time lock to deadlock against.

**Sent vs failed.** The row is opened *before* the push goes out, because its id
has to be in that device's payload. An empty `error` means the push left; a
non-empty one is what the push service said when it rejected the message. A
404/410 still prunes the subscription, exactly as before. Every ledger
operation is best-effort inside its own savepoint: a ledger that can't be
written never costs the athlete their notification, and there's a test that
makes the write raise and asserts the push still went.

**Clicks are recorded on the landing page, not from the service worker.** The
notification's URL carries `?n=<row id>`; when the row's own owner GETs that
page, `notifications.push.record_push_click` stamps `clicked_at` and emits
`push_clicked`. A service worker has no CSRF token and no session it can be
trusted with, and a public click endpoint would be a free counter for anyone.
The write is a conditional UPDATE on `clicked_at IS NULL` scoped to the
requesting user, so a reload, a shared link, or two simultaneous loads count
once; an id that is unknown, malformed, or someone else's is ignored in
silence. An inline script drops the parameter from the address bar with
`history.replaceState` once the server has counted it. A speculative fetch —
`Sec-Purpose: prefetch`, or the older `Purpose: prefetch` an iOS link preview
sends — is skipped, so a link the athlete never tapped can't burn the row's
one shot. `/meso/me/` is the only URL a push targets today, so that is the only
view that calls the helper.

**What a per-send parameter did to the service worker.** Three separate things
in `sw.js` assumed the notification's URL was a stable, bare `/meso/me/`, and
all three broke quietly when it stopped being one; the review caught them and
`PWA_CACHE_VERSION` went to `meso-pwa-v5` so installed clients pick up the fix.
Cache reads and writes now go through an `n`-stripped key, because Cache
Storage matches on the full URL: otherwise every tap stored a whole extra copy
of the athlete home under a key nothing would ask for again, while the plain
`/meso/me/` entry — the one the offline fallback looks for — went stale, so an
offline tap landed on the offline page instead of the cached home. And
`notificationclick` now matches an open window on **path** rather than on the
whole URL (an open tab's URL can never contain a fresh `?n=`, so it spawned a
second window every time beside the running PWA), then *navigates* that window
to the full target rather than merely focusing it — a bare `focus()` would have
shown the right page and counted nothing. `meso_track.js` also joined the
precache list, beside the two athlete scripts that call it.

What this can't see: a notification the athlete reads and swipes away is not a
click, and neither is one tapped on a device that can't reach us. One row per
device, so an athlete with two subscribed devices is two sent rows for one
delivery — and *at most* one row, since a ledger insert that fails is swallowed
and that push goes out without an id, untrackable but sent. The ledger's bias
is optimistic in one more place: a blank `error` means "the push left", so a
rejected push whose error we then failed to write reads as delivered. A third
"unknown" state would fix it and isn't worth a column yet. For a staff or
sandbox athlete the click UPDATE still lands while `track()` drops the event,
so the row reads clicked and never yields its `push_clicked`; harmless, since
the dashboard excludes the same people from both numbers.

A sandbox coach never reaches any of this: `_notify_athlete_block_delivered`
returns before it notifies when the plan's coach is a sandbox, and that is the
only path to `_fan_out`. Worth stating because the Push table's exclusion is a
read-time join to a user row the sandbox reaper deletes, so if a sandbox push
ever *were* written, reaping would launder it into a real send — the same shape
as the subject-match problem the dashboard slice hit. The gate upstream is what
keeps that hypothetical.

**Retention.** Push rows follow the same 13-month rule as `Event`, swept by the
same daily `analytics-purge-expired-events` schedule — the
`analytics_purge_events` command now sweeps both ledgers rather than a second
schedule existing for one small table. `SentEmail`/`EmailEvent` are untouched.

## Product analytics dashboard (#509)

`/meso/analytics/` (`ProductAnalyticsView`, `presenters.product_analytics`) is
the staff read-out of Meso usage, gated like the other staff dashboards
(anonymous → login, non-staff → 403). `?days=7|30|90`, default 30, parsed like
the email dashboard's. A row is in the window when `now - days <= ts <= now`.

**Superseded by #613:** The gate is now `is_superuser`; `handle_no_permission` is unchanged, and the nav links plus `/backside/` Dashboards module use the same gate.

**Sources.** A number comes from the table that already records the fact with
a timestamp, and from `analytics.Event` only when nothing else does. Events
exist only since the slice-1 deploy (2026-09-19), so an `Event`-sourced number
starts there; the page prints the date of the oldest event it holds. Every
feature row names its source.

**Who is excluded, everywhere.** Staff and sandbox users, in the query itself
(a NULL user, from a deleted account, stays in "times" counts). The rule is on
the person being counted, as in `track()`: a staff coach's clients still count
as athletes, because the owner coaches real people. A funnel row is a pair,
so it's left out when either side is staff or sandbox; for an email invite
nobody has accepted, only the coach is known. For email
that means recipients: `SentEmail` doesn't record the sender, so a staff
coach's invite to a real person counts. Activity on a demo relationship's
plan (`is_demo`) is excluded through the join for source tables. For events,
the coach-side events about a plan (plan_created, template_imported,
agent_proposal_run, batch_applied, block_delivered) carry `props.demo` from
this slice on, because "Remove demo data" deletes the demo plans and a match
on the event's subject (`meso.plan`, `meso.mesocycle`,
`meso.agentproposalbatch`) finds nothing afterwards; the subject match stays
for events written before the prop. An athlete is someone another coach
coaches (an active or ended link that is neither self-coaching nor demo), so a coach training
only on their own program is never an athlete, even after the log an event
pointed at is deleted (clearing a typed line reaps an empty log). Their edits
and deliveries still count as coach activity.

**Active users.** WAU (last 7 days), MAU (last 30) and the selected window,
for coaches and for athletes, each one `COUNT` of distinct users. An active
coach edited a plan (`PlanAction`, `Plan.created`), delivered a block
(`WeekDelivery`), ran the agent (`AgentProposalBatch`, eval runs excluded),
sent an invite (`CoachInvite`), or has a coach-side event (plan_created,
template_imported, agent_proposal_run, batch_applied, block_delivered,
invite_sent). An active athlete created a session log that holds a set
(`SessionLog.created_at`), or has a `set_logged` or `session_opened` event.
`SessionLog.last_activity_at` isn't used: migration 0046 stamped every
existing log with its own run time.

**Activation funnel.** Cohort: invites sent in the window. Two paths and their
union. *Email invite* reads `CoachInvite` (sent `created_at`, accepted
`responded_at` when `accepted`; a resend keeps `created_at`, so a resent
invite is timed from its first send). *Athlete request* reads `CoachAthlete`
rows with `invited_by=athlete`, minus any row that an email invite in the same
cohort claimed (claiming an invite can accept a pending request, and it must
not count twice). This answers #542's question about coach-accepted requests
from the source table, with no new event. It's exact for a pair that connects
once. `CoachAthlete` keeps one row per coach and athlete, and a repeat request
or a `relationship_reinvite` reopens it: the row keeps its first `created_at`
and takes the new status and `responded_at`, and a coach re-invite rewrites
`invited_by`. So a pair that reconnects is measured from its first request,
and a coach re-invite moves the pair out of this path. Coach re-invites
themselves aren't in the funnel, since their send time isn't recorded. After acceptance, the
first block is the earliest `WeekDelivery` on that relationship's plans at or
after acceptance, and the first set is the earliest session log with a set by
that athlete on those plans at or after that delivery, timed by when the log
was started (`LoggedSet` has no timestamp of its own). A set logged before any
delivery (possible, since delivery isn't a visibility gate) doesn't count, so
the steps stay sequential. Medians are between consecutive steps. The cohort
rows are the only ones loaded into Python, because SQLite has no percentile
function; they're bounded by invites sent in the window.

**Feature adoption.** Per feature, distinct users and times in the window:
New program (`plan_created` event), Draft with AI and Agent run
(`AgentProposalBatch` by trigger), Agent changes applied (`batch_applied`
event; the batch has no applied-at time), Template imported (event), Block
delivered (`WeekDelivery`, one per block: its week rows share one
`delivered_at`), Invite sent (`CoachInvite`; a resend reuses the row and isn't
counted again), Trial started (`CoachSubscription.trial_end` minus the 14-day
trial), Pro subscription started (`subscription_started` event with
`via=stripe` — fires when the Stripe subscription is created, including a
coach who subscribes during the trial and isn't charged until it ends),
Subscription cancelled (event), Push notifications enabled
(`PushSubscription`, athletes; an unsubscribed or rejected device's row is
deleted, so this counts subscriptions still on record, dated by the device's
first registration even if it later changed hands), Session completed
(event, athletes), App installed (`pwa_installed` event) and Push permission
granted (`push_permission` event with `result=granted`). A deleted account's
events stay in "times" whatever plan they were about: the account's links,
plans and logs go with it, so a self-coach's completions can't be told apart
any more.

The last two rows are the beacon's, and their "who" is **anyone**, not
athletes. The beacon fires from the athlete surface, which a self-coaching
coach uses too, and the event carries no subject to attribute, so there is
nothing to split the roles on. Read them with the beacon's own counting rules
in mind (see "First-party usage events"): one install per browser profile, so
the same person on a phone and a laptop is two; and a permission answer only
when the athlete actually answers a prompt, so a device that decided before
this shipped is invisible.

**Email.** The four Meso kinds (block_delivered, coach_invite,
invite_reminder, coach_request), as a cohort: messages sent in the window
(`SentEmail`), and how many of them were delivered, opened or clicked at any
time since (distinct messages with at least one such `EmailEvent`). That's
unlike `/backside/email/`, which counts events by when they happened; the page
links there for detail. An open can be a mail client's privacy prefetch.
Block delivered includes the email a self-coaching coach gets for their own
block.

**Push**, in the same card, as its own table — push has no delivery receipt
and no open, so folding it into the email columns would have meant printing
"—" in two of them and inviting the reader to compare numbers that aren't the
same measurement. Columns are Sent, Failed, Clicked and click rate, over
`PushNotification` rows sent in the window (see "Push notification ledger"):
sent is the pushes the push service took, failed is the ones it rejected, and
clicked is those whose link was opened at any time since. One row per device,
so an athlete with two subscribed devices is two sent rows for one delivery,
and a notification read and swiped away is not a click.

**Retention.** Raw `Event` rows and `PushNotification` rows older than 13
months are deleted daily by the `analytics-purge-expired-events` schedule
(`analytics.tasks` → `analytics_purge_events` →
`analytics.retention.purge_expired_events` and
`notifications.retention.purge_expired_push_notifications`), registered in
`analytics.0002`. Both ledgers ride the one schedule rather than push getting
a second one for a much smaller table. It deletes 1,000 rows per statement
until none are left, so a large backlog never holds one long transaction, and
reports the rows the deletes removed. There's no rollup table: nothing on the
page is slow yet. Add a nightly rollup only when a query measurably is.

---

## Row-lock order (#558, #559, #562)

**One order for the whole app, and it is this:**

```
User  →  CoachAthlete  →  Plan  →  AgentProposalBatch
      →  Week / SessionSlot / ExerciseSlot  →  Session
      →  Prescription  →  SessionLog / LoggedSet
```

and **ascending pk within a table** whenever a path locks more than one row of
one table. `clear_demo` is the explicit #590 exception: it locks the coach's
`User` row first as the per-coach mutex, then locks the demo athletes' `User`
rows ascending. That combined User sequence is not globally pk-sorted. It is
safe against every path that takes the coach row first — the segment loaders,
`plan_create`'s draft path, the sandbox reap — and, as of #610, `UserAdmin` and
`merge_users`: those delete paths first lock every selected coach ascending,
while holding no athlete row, then run the ordinary sorted cascade pass. If
`clear_demo(C)` owns C, that first pass holds nothing it needs; if the delete
owns C, clear cannot pass its first lock. When C is not selected, neither path
wants it and both take their shared athletes ascending. No cycle can form —
provided a demo athlete is never itself a coach. They are created with an
unusable password and no request path gives one a `CoachProfile` or a link as
coach, so only a staff-made row breaks that; a coach that sorts below its own
owner would be locked first and reopen the cycle (#614).

Every path that takes two or more row locks takes them in that sequence,
counting both `select_for_update` and the implicit exclusive lock an
`UPDATE`/`DELETE` takes on the row it writes. A path may **skip** a level. What
it may never do is take two of these in the reverse order. A total order plus
"don't invert it" is what makes a lock cycle unconstructible, so PostgreSQL
never has a deadlock to resolve by aborting somebody's request with a 500.

**Skipping a level is safe only against other paths that respect the order.**
This is the part that is easy to get wrong, so it is stated separately. A
cascade delete runs the whole order *backwards* — `Collector.delete`
fast-deletes children before it updates or deletes parents — and the only thing
that makes it safe is holding the parent lock for the whole delete. So a path
that skips `Plan` is fine against a designer save, and is **not** fine against a
delete of that plan's tree: nothing is left to serialize them. Two consequences,
both load-bearing:

- every hard delete must pre-lock its cascade through the matching
  `demo.lock_cascade_*` helper;
- every athlete write path must take the `Plan` row first, even when its own
  mutation lives below `Session`;
- every creator of a `Plan` must first lock its `CoachAthlete` parent, and every
  creator of a `CoachAthlete` must first lock its parent `User` rows.

**Strength, not just order.** Default to `select_for_update(no_key=True)`.
`FOR NO KEY UPDATE` conflicts with every other writer's `FOR UPDATE` and
`FOR NO KEY UPDATE`, so the exclusion this order needs is real, but it does
**not** conflict with the `FOR KEY SHARE` a transaction takes on a parent row at
COMMIT to check a deferred FK — Django emits every PostgreSQL FK `DEFERRABLE
INITIALLY DEFERRED`, so that lock is taken at commit, *after* the transaction
already holds its own rows, and therefore in an order no code can arrange. That
is what makes it dangerous: a plain `FOR UPDATE` held across a later lock
acquisition turns it from a hazard into a deadlock generator. #560 was that bug
on a user row; #559's first attempt reintroduced it one level up by locking
`User` with `FOR UPDATE` and then reaching for `CoachAthlete`.

Take plain `FOR UPDATE` only when the path needs to conflict with exactly that
commit-time `FOR KEY SHARE` — #584's purge, which must see a `LoggedSet` whose
writer is already inside its COMMIT. "I am about to delete this row" is **not**
a sufficient reason: the `DELETE` takes its own `FOR UPDATE` when it runs, and
reserving the row earlier at that strength buys ordering you already have while
blocking every deferred FK check in the meantime.

One thing `no_key` does not change: an `UPDATE` that rewrites a column in a
unique index (a `token` rotation in `CoachAthlete._open` or
`CoachInvite.resend`) takes `FOR UPDATE` on that row at write time regardless of
the explicit lock. What the explicit `no_key` buys those paths is that the
strong lock is taken only at the write, after their parents, instead of at the
top of the transaction. It is safe for the same reason the rest of the order is:
every `Plan` creator locks its link first (#596), so nothing is mid-insert
against that link holding a pending `KEY SHARE` for the write to wait on. The
#611 sweep applied this test to all fifteen plain sites and none needed to stay
plain; the only plain `Prescription` locks left are `history.py`'s (#584) and
`views._upsert_parsed_set`'s, whose strength #611 deliberately left alone.

**Why this order and not another.** Any total order prevents deadlock; this is
the one the code already mostly followed. The coach's write paths open with the
`Plan` row (`history.record_plan_action`, `api_plan_undo`, `api_plan_redo`), and
`history.restore_plan_snapshot` walks down through `Week` → `SessionSlot` →
`ExerciseSlot` → `Session` → `Prescription` (#584/#583 kept that sequence; it is
adopted here as the app-wide one). `Week`, `SessionSlot` and `ExerciseSlot` sit
at one level on purpose: every writer of them already holds the `Plan` row, so
their relative order cannot produce a cycle, and pinning one would make the
restore's existing sequence a violation for no gain.

### What conforms today

- `api_plan_undo` / `api_plan_redo` → `history.restore_plan_snapshot`, and every
  designer endpoint through `history.record_plan_action`.
- `views.athlete_cell_write` — takes the `Plan` row first as of #562. It used to
  take the `Session` lock and then write the `Plan` row via `_touch_plan`, which
  inverted against the restore.
- `views.athlete_log_session` and `settle.settle_log` — take `Plan` first, then
  lock only the `Session` row with `of=("self",)` (#588). The latter clause
  prevents `Session.Meta.ordering` from also locking the joined `SessionSlot`.
- `views.cell_line_write` — takes the `Plan` row before its reclaim write as of
  #562. Its `existing.save(...)` made it the one path running
  `Prescription` → `Plan`.
- `views.batch_apply` — takes the `Plan` row before the batch as of #559. It ran
  batch → `Plan` (via `record_plan_action`), which only became reachable once
  `lock_cascade_parents` started going parent-first.
- #611's strength sweep uses `FOR NO KEY UPDATE` for the row mutexes in
  `CoachSubscription.start_trial_for`, `views.plan_create`,
  `relationship_reinvite`, `athlete_request_coach`, the CoachInvite
  revoke/resend/claim transitions, `session_add`, `week_add`, `batch_apply`,
  `batch_dismiss`, `change_set_status`, `agent.service._still_resolvable`,
  `billing.webhooks._lock_mirror`, and its invoice nudge. The querysets are
  unjoined, so none needs `OF`; `_upsert_parsed_set` separately uses
  `of=("self",)` on its deliberately plain Prescription lock so
  `Prescription.Meta.ordering` cannot also lock the joined ExerciseSlot.
- `views.change_set_status` — batch, then its `ProposedChange`. Skips `Plan`
  legitimately: it never touches that row, and no delete path can race it
  without holding the batch lock first.
- `agent.service._persist_result` / `_fail` — batch, then its children (#558).
- `views.plan_create` — `CoachAthlete`, then `Plan`. Conforming as of #559,
  which extended this order upward to cover it. Its `draft=1` path takes the
  coach's `User` row *first*, before the link: `_reserve_plan_draft` used to lock
  it after the link and the new plan, which inverted, and stayed harmless only
  while the sandbox reap was the one path locking a coach's `User` row and then
  their links. #590 made `clear_demo` a second such path, so the draft now
  reserves the coach up front (a later re-acquire in `_reserve_plan_draft` is a
  no-op).
- `views.template_use` / `plan_batch_deliver` lock their target links before
  duplicating a plan; `roster_add_self` / `relationship_reinvite` take the
  coach `User` first; `invite_claim` accept takes both participant `User` rows
  ascending before its invite row (#596); `athlete_request_coach` takes both
  participant `User` rows ascending before its link, and `coach_invite` takes
  the coach `User` row before opening an invite (#611).
- `demo.clear_demo` takes the coach mutex before reading its athlete set, then
  uses `demo.lock_cascade_parents`; `sandbox.expire_sandboxes` safely re-locks
  that same coach row inside its later delete transaction (#590).
- `demo.lock_coach_mutexes` identifies selected coaches through unjoined
  subqueries and reserves their `User` mutex rows ascending before a multi-User
  hard delete's sorted cascade pass (#610).
- `UserAdmin`, `CoachAthleteAdmin`, `PlanAdmin`, `AgentProposalBatchAdmin`, and
  `merge_users` wrap their hard delete and the matching
  `demo.lock_cascade_*` helper in one transaction; the two User-rooted callers
  take the #610 coach-mutex pre-pass first (#587).

### Known gaps and deliberately unswept sites

The #587/#588/#589/#590/#596 reachable cycles above are closed. The remaining
inventory is explicit rather than implied to conform:

- The plain `Prescription` locks in `history.py` are deliberate: #584's purge
  must conflict with a commit-time `FOR KEY SHARE`.
- The `CoachAthlete._open` / `invite` / `request` / `add_self` and
  `CoachInvite.open_for` model helpers stay lock-free by design; every
  request-reachable caller now holds the required parent `User` rows first.
  The remaining unlocked creators are `seed_meso_demo`, an offline management
  command with no concurrent request surface, and `demo._ensure_demo_link`,
  whose segment loader holds the coach `User` mutex and is `clear_demo`'s
  documented #590 exception.

## Decision log

_(Append dated entries here as decisions land.)_

- 2026-06-26 — Doc created; B1, B2, B4 and the build-order question raised for decision.
- 2026-06-26 — **Decided:** B1 = multi-coach SaaS from day one · B2 = athletes are Users who
  log in (web/PWA), coach can edit their plan · B4 = hybrid exercise source · first slice =
  persistence. Multi-coach promoted N1–N4 into scope. **Open before schema:** N1 cardinality.
- 2026-06-26 — **Decided:** N1 = many-to-many, athlete-consented (an athlete may work with
  multiple coaches; either party can end it). Plans owned per coach↔athlete relationship (D-a);
  contraindications global, goals per-plan (D-b); bidirectional invites (D-c). Schema is now
  unblocked.
- 2026-06-26 — **Confirmed (build):** role/profile models live in the **`meso`** app (not
  `users`); PR 1 ships the invite **state machine + tokened URLs**, with real email send as a
  follow-up. **Phase 1 built** on branch `meso-persistence-phase1`: `CoachProfile`,
  `AthleteProfile`, `Contraindication`, `CoachAthlete` (M2M-through + state machine + scoped
  manager); migration `meso.0001_initial`; admin; factories; 28 tests; roster + athlete-profile
  read real scoped data. Athlete-profile URL is now `<uuid:pk>` (was a mock slug).
- 2026-06-27 — **Phase 1 merged & deployed:** PR #270 squash-merged to `main` (`ec06974`),
  Django CI green, deployed to Hetzner (migration applied in prod). Resume point → Phase 2
  (program schema).
- 2026-06-27 — **Phase 2 built & merged** (PR #271, squash `079b891`; Django CI green): the program
  schema `Plan → Mesocycle → Week → Session → ExercisePrescription` (hybrid catalog `Exercise` FK,
  nullable = B4) + `PlanQuerySet` scoping (D-a) + `SessionLog`/`LoggedSet` (models now, UI later)
  in `meso/models.py`; `CoachAthlete.end()` now archives the relationship's plans (D-c); a
  `serialize_plan` (`meso/serializers.py`) round-trips a seeded plan to the designer's
  `program`/`weeks`/`phases` shape (macrocycle phase state derived by sequence position, not
  `order` arithmetic — robust to non-contiguous order). Migration `meso.0002`; admin + factories;
  built red→green then a local Codex review pass (19 new tests, 47 meso / 187 project-wide).
  Resume point → Phase 3 (designer save/load). Settles B3 in build form: distinct entities, full
  hierarchy; draft/active/archived status on `Plan` (no separate `ProposedChange` yet — that lands
  with the agent slice).
- 2026-06-27 — **Phase 5 built & merged** (PR #278, squash `5babceb`; Django CI green, deployed to
  Hetzner): `seed_meso_demo` management command (idempotent; `--delete` / `--coach-email`; created
  coach gets a runtime-random password, no literal in source) stands up the coach + five athletes +
  active links + Maya's sample plan as real rows, reproducing the prototype roster/designer. The
  coach-side **mock is retired**: bare `/meso/designer/` + `/meso/deliver/` redirect to the coach's
  working plan (`_coach_working_plan`) or the roster, `DeliverView`'s `mockdata.DELIVER` fallback is
  gone, and `meso.js`'s `program`/`weeks`/`phases` fixtures are emptied (the grid always hydrates
  from an injected plan). `mockdata.py` now serves **only** the review + results screens (their own
  slices). Built red→green then a 3-round local Codex review (reseed-reconcile + redirect ordering):
  20 new tests, 99 meso / 239 project-wide green. No migration (no model changes). The designer's
  left-rail/agent/phone chrome stays static prototype HTML by design — it rebuilds with the agent +
  athlete slices. **Persistence slice complete.** Resume point → the **agent** slice (B6: proposal
  engine behind the review gate).
- 2026-06-27 — **Agent Phase 1 built & merged** (PR #280, squash `953d9d4`; Django CI green, deployed
  to Hetzner — migration `meso.0004` applied): the **B6 proposal engine behind the review gate** is
  live. `AgentProposalBatch` + `ProposedChange` + the `meso/agent/` package (`client`/`validation`/
  `service`); `POST api/plan/<id>/agent/` runs Claude (`claude-opus-4-8`, forced `propose_program_changes`
  tool + prompt caching; **adaptive thinking omitted** — incompatible with a forced `tool_choice`),
  validates server-side, and persists a reviewable batch; read-only `GET review/<batch_id>/` renders
  it. **Contraindications enforced in a deterministic validation layer** (current-week scoping,
  target consistency, swap-only contraindication backstop, plural-folded), not just the prompt;
  human approval gate unchanged. 47 new tests (146 meso / 286 project-wide); local Codex review clean
  (8 rounds). Build plan + phasing in [`agent-plan.md`](../archive/meso/agent-plan.md). Resume point → agent Phase 2
  (per-change approve/reject + **apply** back into the program).
- 2026-06-27 — **Agent Phase 2 built & merged** (PR #282, squash `ee7d456`; Django CI green, deployed
  to Hetzner — **no migration**, `status`/`payload` already existed): the review gate now **writes
  back**. `meso/agent/apply.py` applies each approved change's structured `payload` (swap → prescription
  name; progress → load; volume → set count; deload → flags the week), built deterministically by
  `agent.validation` from the tool's new `new_name`/`new_load`/`new_sets` fields. Endpoints (scoped to a
  coach-owned batch): `POST api/change/<pk>/status/` persists per-change approve/reject;
  `POST api/batch/<id>/apply/` applies every non-rejected change in one transaction → batch `applied`,
  bumps `Plan.modified`; `POST api/batch/<id>/dismiss/` → `dismissed`. `review.html` persists toggles
  and wires Apply/Discard; bare `review/` redirects to the latest pending batch and `mockdata.PROPOSED_CHANGES`
  is retired. No migration (status/payload already existed). +33 tests (179 meso / 319 project-wide).
  Resume point → agent Phase 3 (designer agent-chat column).
- 2026-06-27 — **Agent Phase 3 done & merged** (PR #284, squash `5bfe754`; Django CI green, deployed to
  Hetzner — no migration; deployed `meso.js` serving the new chat confirmed live): the designer's
  agent-chat column goes **live**. The canned keyword intent engine (`detectIntent`/`applyIntent`/`dispatch` in `meso.js`,
  which matched the coach's text to one of four scripted edits and mutated the grid in place) is retired;
  a coach turn — typed or via a chip — now POSTs to `api/plan/<id>/agent/` (the Phase 1 endpoint) and the
  returned batch renders inline (per-change `title`/`before`→`after` under the summary) with a
  **"Review N changes →"** link to the review gate. The agent only **proposes** — the chat never mutates
  the program grid; changes stay inert until applied at review. Friendly fallbacks for 503/502/400/network
  errors; composer + chips disable while drafting. No backend change (the endpoint already existed); chat
  is **not persisted yet**. Tests (`test_designer_agent_chat.py`): no JS runner in-project, so they guard
  the retirement + real wiring at the source level + a render check. +8 tests (192 meso / 332 project-wide);
  local Codex review clean (1 round). Resume point → agent Phase 4 (background job + streamed status +
  golden eval cases).
- 2026-06-27 — **Agent Phase 4 built** (execution + eval; branch `meso-agent-phase4`): the proposal run
  moved **off the request thread**. `agent/service.py` split into `create_drafting_batch` +
  `run_proposal_job` (never raises — flips a `drafting` batch to `pending`/`failed`); `agent/jobs.py`
  dispatches it in a daemon thread deferred to `transaction.on_commit` (ATOMIC_REQUESTS visibility), with
  `MESO_AGENT_RUN_SYNC` for inline/deterministic test runs. The endpoint returns **202** + a `status_url`;
  the designer chat **polls** `GET api/batch/<id>/status/` (`meso.js` `pollBatch`) keeping the "drafting…"
  state up until the batch lands. **No real task queue** — a daemon thread is right-sized for a single
  short call behind the human gate on this box (Redis is cache/sessions only); a worker queue is a drop-in
  later behind `run_proposal_job`. Schema `meso.0005` (batch `drafting`/`failed` + `error`). **Logged
  sessions feed grounding** (`build_context.recent_logs` via `serialize_recent_logs`). **Golden evals**
  (`agent/evals.py` model-agnostic invariants responsive/grounded/safe + `manage.py meso_agent_eval`,
  side-effect-free, `--dry-run` without a key). Built red→green (+40 tests). **Closes the B6 agent slice**
  (only persisted chat thread + athlete-facing surfaces remain, both later slices).
- 2026-06-27 — **Agent Phase 4 merged & deployed** (PR #286, squash `82fd360`; Django CI green, deployed to
  Hetzner — migration `meso.0005_agentproposalbatch_error_and_more` applied in prod; local Codex review
  clean, 1 round; +40 tests, 219 meso / 359 project-wide). **The B6 agent slice is complete.** Resume point
  → either a **persisted chat thread** (saving the designer conversation, deferred since Phase 3) or the
  **athlete-facing slice** (delivery + logging PWA, then results feeding back to the agent — decisions S3/S7).
- 2026-06-27 — **Athlete-facing slice started** (decision: build item 3 of the suggested sequence — the
  athlete surface — over the persisted chat thread). Plan + phasing in [`athlete-plan.md`](../archive/meso/athlete-plan.md)
  (Phase 1 read surface · Phase 2 logging · Phase 3 results-feedback · Phase 4 PWA + notifications, S3/S7).
  **Phase 1 built** (branch `meso-athlete-phase1`): the athlete's own read surface — `AthleteHomeView`
  (`/meso/me/`) lists their active-coach, non-archived plans with each plan's latest **delivered** week +
  sessions (done/pending from the athlete's own `SessionLog`); `AthleteSessionView` (`/meso/me/session/<id>/`)
  renders one delivered session's prescribed grid read-only. Athlete-side scoping (`_athlete_plans` /
  `_athlete_session_or_404`) mirrors the coach's `_coach_plan_or_forbidden`: out-of-scope (foreign athlete /
  undelivered week / archived plan / unknown id) is a flat 404. Role-aware nav (overridable `navlinks` /
  `topnav_avatar` blocks; pure athletes redirected off the coach roster). **No model change / no migration**
  (`SessionLog`/`LoggedSet` already exist; B2 confirms athletes are Users who log in). Settles the **delivery
  contract**: delivery gates a week's *visibility*, contents stay live, the `WeekDelivery` snapshot is the
  deferred-diff record (Codex-review nit resolved this way — see `athlete-plan.md` design note). Built
  red→green: +20 tests (239 meso / 379 project-wide); local Codex review clean (1 round). Resume point →
  athlete Phase 2 (session logging — the write path that produces the rows `serialize_recent_logs` grounds
  the agent on).
- 2026-06-27 — **Athlete Phase 1 merged & deployed** (PR #288, squash `42bb805`; Django CI green, deployed
  to Hetzner — **no migration**; `/meso/me/` + `/meso/me/session/<id>/` live and login-gated in prod). The
  athlete read surface is live. Resume point → **athlete Phase 2** (session logging — the write path).
- 2026-06-27 — **Athlete Phase 2 built** (branch `meso-athlete-phase2`): the **write path** —
  `athlete_log_session` (`POST /meso/api/me/session/<id>/log/`) upserts the logged-in athlete's own
  `SessionLog` + `LoggedSet` rows (most-recent-wins, idempotent), flips the session done, stamps the date.
  Scoped by the read surface's `_athlete_session_or_404` (foreign/undelivered/archived/unknown → flat 404),
  **validated before any write** (`_clean_logged_sets`; bad input is a 400 that persists nothing), wrapped in
  a transaction (replace, not append, the set rows). The session screen becomes the interactive logger
  (`presenters.athlete_session` pre-fills set rows from the existing log carrying the coach's full target;
  `athlete_log_payload` + `athlete_session.html` + `meso_athlete.js`). **These are the first real rows
  `serialize_recent_logs` grounds the agent on** — the agent slice already consumed `recent_logs`, this
  produces them. **No model change / no migration** (`SessionLog`/`LoggedSet` already existed). Built
  red→green: **+33 tests** (272 meso / 412 project-wide), ruff clean. **Local Codex review: 0 blocking
  across 5 rounds**; nits fixed (set_number bound + render cap, duplicate-key reject, prescribed load/RPE
  shown, Save-progress keeps done, row-state sync, **workout-date preserved on later edits**). **Declined by
  design:** a `unique(session, athlete)` constraint — the model intentionally permits multiple logs per
  athlete/session (dated history that `serialize_recent_logs` + `test_recent_logs_are_capped_and_newest_first`
  rely on); the constraint would break grounding, and the re-save path is already idempotent. Resume point →
  **athlete Phase 3** (results feed back: retire `mockdata.RESULTS_*`, light up the designer's `last`/`adj`
  from real logs).
- 2026-06-28 — **Athlete Phase 4b built — PWA + web push (closes S7, and the S3 push half deferred from 4a)**
  (branch `meso-athlete-phase4b`). The athlete surface is now **installable, offline-tolerant, and
  push-capable**. *Installable (S7):* the manifest (`/meso/manifest.webmanifest`) and service worker
  (`/meso/sw.js`) are served as **views, not static files** — WhiteNoise's `CompressedManifestStaticFilesStorage`
  hashes static filenames (unstable URL + wrong scope for a worker), and a worker only controls pages at/below
  its own path, so it lives at `/meso/sw.js` to scope `/meso/`. The worker template resolves the **hashed**
  precache URLs via `{% static %}` so the cached shell auto-busts every deploy. Strategy: precache shell +
  offline page on install; network-first navigations (fall back to last-good cached page → offline page);
  stale-while-revalidate static GETs; POSTs pass through (the page owns offline writes). *Offline logging (S7):*
  `meso_athlete.js` stashes a save that fails on an unreachable network into a localStorage outbox (one per
  session, latest wins) and flushes it on `online`/load — safe because the log endpoint is idempotent (pinned
  by `TestOfflineReplayIsIdempotent`). *Web push (S3):* a `PushSubscription` model (**the one migration this
  slice**, `0006_pushsubscription`) the browser registers via `api/me/push/subscribe/` (caller-scoped, upsert
  by endpoint); `meso/push.py` signs with VAPID (`pywebpush`, **new dep**) and `notify_week_delivered` pushes
  the deliver payload to the athlete's devices — prunes a 404/410-Gone endpoint, swallows other failures, and
  is a **silent no-op without `MESO_VAPID_*` keys** (same graceful degradation as the no-address email). The
  deliver hook fires email **and** push, each independently best-effort on `on_commit`; athlete-only. PWA chrome
  + push subscribe-flow ride an athlete-only `pwa` block (`_pwa_head.html`), so **coach screens stay plain web**.
  Built red→green: **+43 tests** (`test_athlete_pwa.py`, `test_push.py`; 354 meso / 494 project-wide), ruff
  clean. Deployment verified locally (collectstatic + hashed SW precache under manifest storage,
  `makemigrations --check`). **Activating push in prod needs `MESO_VAPID_PUBLIC_KEY` / `MESO_VAPID_PRIVATE_KEY`
  / `MESO_VAPID_SUBJECT` provisioned** (see `docs/deploy-hetzner.md`); the migration + deploy succeed without
  them (push just stays dormant). Resume point → **the athlete slice is feature-complete** (install + offline
  logging + delivery email/push + results feeding the coach & agent). Open follow-ups: Background Sync,
  re-deliver push debouncing, in-app notification settings.
- 2026-06-28 — **Persisted designer chat thread built** (branch `meso-chat-thread`; the loose end deferred
  since agent Phase 3/4). The designer's agent conversation now **survives a reload**. Key realization: the
  thread is **already persisted, losslessly**, in the `AgentProposalBatch` rows — each coach turn is a batch
  (`instruction` = the coach's message, `summary` + the `ProposedChange` rows = the agent's reply,
  `status`/`created_at` = state + order), and the agent never sends free-form chat, so the batches **are** the
  thread. So we rebuild it rather than adding a model: `serializers.serialize_chat_thread(plan)` expands the
  plan's batches (oldest first) into the exact `meso.js` `messages` shape (failed → an error note; drafting →
  a neutral note carrying a `pollUrl`; else the summary + inline changes + a review link), the designer view
  injects it via `json_script`, and `meso.js` `hydrateThread()` replaces the lone greeting (kept only for an
  empty history), scrolls to the latest turn, and **resumes polling** a still-drafting run so a reload mid-run
  doesn't go stale. **No model, no migration** — same "reuse what exists, defer new tables" taste as the
  athlete slice. Built red→green: **+16 tests** (`test_chat_thread.py`; 379 meso / 519 project-wide), ruff
  clean. **Local Codex review: 0 blocking across 3 rounds → CLEAN** (two nits fixed: the drafting resume-poll
  and the scroll-to-latest). Plan in [`chat-thread-plan.md`](../archive/meso/chat-thread-plan.md). **Deferred:** a dedicated
  `ChatMessage` model (only if the agent ever sends text not tied to a batch) · editing past turns ·
  pagination of a very long thread. Resume point → next-slice options: **groups (S1)** is the main remaining
  Meso feature area.
- 2026-06-28 — **Groups slice (S1) started — Phase 1 built** (branch `meso-groups-phase1`). The
  tenancy-correct **group + membership spine** + the read surface, no shared program yet. `MesoGroup`
  (coach-owned: name, focus, status; `MesoGroupQuerySet.for_coach/active`) + `GroupMembership` (group ↔ an
  **active** `CoachAthlete` link, `unique(group, relationship)`); helpers `add_athlete` (off the coach's
  active link — raises on no-link / cross-coach / self; idempotent), `remove_athlete`, `active_member_users`
  (scoped to *active* links so an ended relationship hides the member **without deleting the row** — reopening
  the link restores them; read-side scoping, not deletion). Migration `meso.0007`. The roster's dead
  `groups = []` lights up off real rows (`presenters.roster_group`), and a coach-scoped `GroupDetailView`
  (`/meso/group/<id>/`) lists members + the folded "flags across group" (`presenters.group_detail`); the
  roster card links to it (was a dead designer link). Admin (group + membership inline), factories, and a
  seeded demo group ("Tue/Thu Strength Squad", 3 members) in `seed_meso_demo` (idempotent; torn down on
  `--delete`). **Membership tenancy is enforced two ways** (both from the Codex review): `active_member_users`
  scopes to `relationship.coach == group.coach` (a row written outside `add_athlete`, e.g. a raw admin inline,
  can't leak a foreign coach's athlete onto the read surface) and `GroupMembership.clean` rejects a cross-coach
  relationship + an inactive link **on creation only** (so a since-ended row stays re-savable). Built red→green:
  **+28 tests** (`test_groups.py` + seed coverage; 407 meso / 545 project-wide), ruff clean, `makemigrations
  --check` clean. **Local Codex review: 0 blocking → CLEAN** (3 rounds; two P2 membership-tenancy nits fixed).
  Plan + phasing in [`groups-plan.md`](../archive/meso/groups-plan.md). Resume point → **groups Phase 2** (the shared group
  program: `Plan.group` FK + nullable `relationship`, the designer's Group mode, create-group UI).
- 2026-06-28 — **Groups slice (S1) Phase 2a built** (branch `meso-groups-phase2a`). The **shared group
  program**: a `Plan` rooted at a `MesoGroup` instead of a `CoachAthlete` relationship. Phase 2 is split (like
  the athlete slice's Phase 4) into **2a** (this — the program spine + Group-mode designer) and **2b**
  (create-group-from-roster UI). `Plan.relationship` is now nullable and `Plan.group` (FK → `MesoGroup`,
  `related_name="plans"`) added, with a `plan_relationship_xor_group` `CheckConstraint` so a plan is rooted at
  **exactly one** of the two — the program tree is reused, gaining only a root (and, Phase 3, an override
  overlay). Migration `meso.0008`. **The load-bearing scoping decision:** `PlanQuerySet.editable_by(user)` is
  the wider designer/autosave gate (individual-active **or** group-owned), while **`for_coach` stays
  individual-only** so the athlete-shaped deliver/results/review flows never see a group plan (which has no
  single `athlete` — `Plan.athlete` returns `None`). `MesoGroup.shared_plan()/create_shared_plan()` (a starter
  scaffold — there's no add-session/week endpoint yet, so a bare plan would be uneditable). The designer opens a
  group plan (`MesoDesignerView` via `editable_by`), `serialize_plan` carries a `group` identity payload
  (`serialize_group_identity`: members + folded flags) and skips the athlete-scoped "last" column; `meso.js`
  hydrates Group mode off it (no more hardcoded squad; fabricated per-athlete adjusts → honest Phase-3
  placeholder). **Deliver + the agent reject a group plan with 400** (Phases 4/3; both deref `plan.athlete`);
  autosave is athlete-agnostic and works. A `group_design` POST entry point + the group-detail "Design / Open
  shared program" card; the seeded demo group gets a shared program. `initials` moved to `serializers` (avoids a
  presenters import cycle). Built red→green: **+29 tests** (`test_group_program.py` + seed coverage; 583
  project-wide), ruff clean, `makemigrations --check` clean. Plan + build notes in
  [`groups-plan.md`](../archive/meso/groups-plan.md). Resume point → **groups Phase 3** (per-athlete overrides: the `adj`
  overlay — `PrescriptionOverride`, effective-program resolution, the designer's per-row `adj` badge), then
  **Phase 2b** (create-group UI) and **Phase 4** (deliver-to-all).
- 2026-06-28 — **Groups slice (S1) Phase 3 built** (branch `meso-groups-phase3`). **Per-athlete overrides —
  the `adj` overlay.** A `PrescriptionOverride(membership FK → GroupMembership, prescription FK →
  ExercisePrescription, swap_name, load_pct, sets, reps, note)` with `unique(membership, prescription)` is a
  thin diff layered on a group's *shared* `ExercisePrescription`: a member's **effective program = shared
  template + their override diffs** (no second hierarchy). It hangs off the `GroupMembership` (so the same
  `CoachAthlete` link that owns the member's individual plans — D-a), guarded by a **same-group invariant**
  (the override's prescription must live in the membership's group's shared program — `set_override` raises
  `InvalidTransition`, `clean` backstops the admin). Migration `meso.0009`. `serializers.resolve_prescription`
  is the pure resolver (swap replaces the name, `load_pct` scales a numeric load 2.5-rounded à la the
  designer's `round25`, `sets`/`reps`/`note` override volume/note); `group_adjustments` is one query over the
  plan's overrides scoped to **active** members → a per-row `adj` summary (`"{initials} {label}"` for one,
  `"N adjusts"` for several) + an `adjusts` breakdown, which `serialize_plan` attaches to group-plan grid rows
  (the group analogue of the individual plan's logged `last` column). `prescription_override` (POST
  `/meso/api/plan/<id>/prescription/<pk>/override/`) sets/clears one member's adjust — group-only (individual
  → 400), coach-scoped (403), prescription-in-plan (404), active-member (400), `load_pct` bounded; the reply
  carries the recomputed row `adj` so the badge repaints. The designer grid renders the badge off real diffs
  (per-athlete breakdown on hover); the seeded demo group gets a few overrides (idempotent). **No in-grid
  override *editor* yet** — the badge renders off seed/admin/API-created diffs; the click-to-adjust UI is the
  immediate follow-up. Built red→green: **+41 tests** (`test_group_overrides.py` + seed coverage; 625
  project-wide), full suite + 30 JS tests green, ruff + format clean, `makemigrations --check` clean. Plan +
  build notes in [`groups-plan.md`](../archive/meso/groups-plan.md). Resume point → the **override editor UI** (click a row
  to set a member's adjust), then **Phase 2b** (create-group UI) and **Phase 4** (deliver-to-all — fan a
  per-athlete *resolved* snapshot out to each member, reusing `resolve_prescription`).
- 2026-06-28 — **Groups slice (S1) COMPLETE** (Phases 2b + 3-editor + 4 all built, merged & deployed — PRs
  #301/#302/#303). The whole Meso feature area (individual coach + agent + athlete PWA + groups) is now real
  & deployed; `mockdata.py` is gone. **Next slice chosen by the user: S2 (units & RPE/%1RM).** Units (kg/lb)
  turned out already shipped with earlier slices (`Unit`/`CoachProfile.default_unit`/`Plan.unit`, threaded
  through serializers/presenters/designer/seed); the remaining gap is **first-class %1RM** — the designer's
  Load number always meant an absolute load, with no way to prescribe "75% of 1RM" (RPE already has its own
  orthogonal column). **S2 Phase 1 building** (branch `meso-units-rpe-phase1`): a `LoadType`
  (`ABSOLUTE`/`PERCENT`) + `ExercisePrescription.load_type` (default ABSOLUTE — data-safe) carried through
  the serializer, the per-athlete override resolver, and the group deliver fan-out; the designer Load cell
  toggles `%` ⇄ the unit and autosaves the type; the athlete sees a `%` target and the coach results screen
  labels a %1RM target with `%`. Migration `meso.0011`. Agent %1RM-awareness deferred to Phase 2 (the agent
  is type-agnostic — a %1RM number progresses as a number). Plan + phasing in
  [`units-rpe-plan.md`](../archive/meso/units-rpe-plan.md).
- 2026-06-28 — **S2 Phase 2a — agent %1RM-awareness** (branch `meso-units-rpe-phase2a-agent`, **no
  migration**). Phase 2 split 2a/2b (groups-slice cadence). The agent grounding already carried each row's
  `load_type` (Phase 1 wired `serialize_prescription`), so the two real gaps were the **prompt** (never
  explained `load_type`) and the **validation backstop** (never bounded a %1RM progression). Closed both:
  `SYSTEM_PROMPT` + the `new_load` tool field now explain `abs` vs `pct` (%1RM); `clean_change` bounds a
  `progress` on a `PERCENT`-typed target to `0 < pct ≤ 120` (rejects an absolute-looking "180" or a
  non-numeric value, normalizes `'82.5 %'` → `'82.5'`), leaving the absolute path unbounded. The agent still
  does **not** change a row's type. Athlete %1RM logging ergonomics remain → **Phase 2b**. Plan in
  [`units-rpe-plan.md`](../archive/meso/units-rpe-plan.md).
- 2026-06-28 — **S2 Phase 2b — athlete %1RM logging ergonomics → S2 COMPLETE** (branch
  `meso-units-rpe-phase2b`, **no migration**). A %1RM target is an *intensity, not a weight*: Phase 1 let
  the athlete *see* the `%`, but converting "75%" to a bar load was still manual. Phase 2b adds an
  **estimated-1RM helper** (% ⇄ load). The data contract is the only backend change — `athlete_session`
  carries the plan's `unit`, and `athlete_log_payload` threads `unit` + each row's structured
  `load`/`load_type` — so the client knows which rows are %1RM. The maths is **client-side** (the athlete's
  1RM estimate is per-device convenience, not coach-owned program data): `meso_athlete.js` gains pure
  helpers (`epleyOneRm` — Epley, single-rep = the load itself; `loadForPercent` — plate-rounded à la the
  designer's `round25`) + component methods (`isPercentLift`/`suggestedLoad`/`setImpliedOneRm`), with the
  estimate persisted in **localStorage** keyed by exercise id (same "defer new tables" taste as the offline
  log queue). The logger renders a `%1RM` badge, a "your 1RM" input, the suggested load (`75% ≈ 90 kg`), and
  a per-set implied-1RM hint — all gated on a %1RM lift (absolute lifts untouched). A `LoggedSet` still
  records the *actual* (absolute) weight. Built red→green: **+3 pytest** (566 meso) + **+14 Vitest** (60
  frontend), ruff + prettier clean, `makemigrations --check` clean. **Local Codex review: CLEAN on
  iteration 1.** **Deferred:** a persisted/coach-visible estimated 1RM (model + migration) and
  auto-deriving it from logged history. **The whole Meso feature area is now real & deployed; S2 is
  complete — no obvious next big slice, ask the user.** Plan in [`units-rpe-plan.md`](../archive/meso/units-rpe-plan.md)
- 2026-06-28 — **S2 follow-up: persisted, auto-derived, coach-visible 1RM — Phase 1 built** (branch
  `meso-one-rm-phase1`). The deferred Phase-2b follow-up: the athlete's estimated 1RM lived only in
  per-device `localStorage`. Phase 1 promotes it to a real **`AthleteOneRm`** row (one per
  `(athlete, lift)`; lift identity = the B4 hybrid `_exercise_key`, denormalized into a `key` with
  `unique(athlete, key)`), **auto-derived from the athlete's completed logged sets** — the best Epley
  estimate per lift (`one_rm.py`: `epley_one_rm` mirrors `meso_athlete.js` exactly, `derive_one_rm_values`
  / `refresh_one_rms` / `one_rm_values`). The log endpoint refreshes the rows on every *done* save;
  migration `0012_athleteonerm` (schema) + `0013_backfill_one_rms` (derives from existing history,
  idempotent). **Two surfaces:** the athlete logger seeds its suggested bar load from the derived value
  (a typed per-device override still layers on top — `effectiveOneRm`), and the coach designer shows a
  `1RM: 140 kg` badge on an individual plan's `%1RM` row (`serialize_plan` threads it; a group plan has
  no single athlete, so none). The seed derives Maya's Box Squat 1RM (84) so the demo shows it. Built
  red→green: **+33 pytest** (`test_one_rm.py`; 600 meso / 740 project-wide) + **+5 Vitest** (65 frontend)
  + a seed assertion, ruff + format clean, `makemigrations --check` clean. Plan in
  [`one-rm-plan.md`](../archive/meso/one-rm-plan.md). **Deferred:** manual entry persisted server-side (a `source` field
  + endpoint — today logs only *raise* the estimate), coach-editable 1RM, smarter derivation / unit
  conversion..
- 2026-06-28 — **S2 follow-up — Phase 2: manual, server-persisted 1RM built** (branch
  `meso-one-rm-phase2`). Closes the first deferred item: the athlete's *typed* 1RM override (per-device
  `localStorage` since Phase 2b — lost on a device change, invisible to the coach, and able only ever to
  *raise* the suggestion) is now a real **`AthleteOneRm.source`** (`logged`/`manual`, default `logged`;
  migration `0014_athleteonerm_source`, schema-only — existing rows were all auto-derived). A `manual` row
  is the athlete's own number: **`refresh_one_rms` skips it** (logs never clobber a manual value, so it can
  sit *below* the heaviest logged set — the thing localStorage couldn't express server-side); a `logged`
  upsert stamps `source=logged`. `one_rm.set_manual_one_rm` upserts the manual row or, on a blank value,
  **clears** it — deleting the manual row and re-deriving from logs immediately so the lift falls back to
  its log-derived estimate (`clean_manual_value` is the reusable validator: blank → clear, positive +
  column-bounded → quantized, else reject). New endpoint **`POST /meso/api/me/session/<pk>/one-rm/`**
  (`{prescription, value}`) scoped exactly like the log endpoint (`_athlete_session_or_404` — a foreign /
  undelivered / unknown session is a flat 404, an out-of-session prescription a 400), returning
  `{one_rm, source}`. The logger payload carries `one_rm_source` + `one_rm_url`; `meso_athlete.js` seeds a
  manual value into the editable input, keeps a logged value as the placeholder, and the input's `@input`
  is now a **debounced best-effort server POST** (`saveOneRm`/`_postOneRm`) — the `meso-e1rm` localStorage
  store is **retired**. Admin surfaces `source` (`list_display` + `list_filter`). Built red→green: **+28
  pytest** (`test_one_rm.py`; 664 meso / 776 project-wide) + **+8 Vitest** (70 frontend, net), ruff +
  prettier + `makemigrations --check` clean. Plan in [`one-rm-plan.md`](../archive/meso/one-rm-plan.md). **Deferred
  (Phase 3+):** coach-editable 1RM (the `source` field already supports it), offline persistence of a
  manual edit, smarter derivation / cross-unit conversion.
- 2026-06-29 — **N4 — athlete onboarding / email invites — Phase 1 built** (branch
  `meso-invites-phase1`). Closes the still-open foundation decision: how an athlete *joins* a
  coach. The Phase-1 peer-invite state machine on `CoachAthlete` required the athlete to already
  be a `User` and nothing in the UI ever *created* an invite, so links were only ever seeded.
  Phase 1 adds the real email-onboarding loop end to end. New **`CoachInvite(coach, email, token,
  status)`** (migration `0015_coachinvite`): a coach invites an *email* (the invitee may have no
  account yet — decoupled from `CoachAthlete`, whose `athlete` FK is non-null, rather than making
  the load-bearing relationship nullable). State machine `open_for`/`accept`/`decline`/`revoke`;
  **`accept(user)` materializes — and immediately activates — a `CoachAthlete`** (the claim *is*
  the acceptance), idempotent against an existing active link, resolves a pending peer link, and
  rejects a coach claiming their own invite; partial-unique `(coach, email)` while pending so a
  re-invite reuses the open row. **Bearer-token claim, no email match** — the 122-bit token in the
  link authorizes (email-only login coexists with social signup, so a new athlete may use a
  different address); the coach sees who accepted and can `end` it. **Rides allauth with no custom
  adapter**: the claim view is `@login_required`, so an anon visitor bounces to
  `/accounts/login/?next=<claim>` and allauth carries `next` through both login and signup
  (`ACCOUNT_EMAIL_VERIFICATION` unset → `"optional"` → signup logs in immediately). Email via
  `notifications.send_coach_invite_email` (+ 3 templates), best-effort on `transaction.on_commit`.
  Roster gains an "Invite an athlete" disclosure + a pending-invite list with Revoke; admin +
  factory + a seeded demo pending invite. Built red→green: **+38 pytest** (`test_invites.py`) + 3
  seed assertions; full suite green (the 2 pre-existing `admin_honeypot` failures are unrelated).
  **Codex review loop CLEAN after iteration 1** (2 fixes: a P1 referrer-leak — moved the
  `no-referrer` meta into a new `_meso_base` `head_top` block that precedes the font `<link>`s; a
  P2 claim race — `select_for_update` on the invite row in the claim/revoke views). Plan +
  deferred (athlete→coach request UI, resend/expiry, stub-athlete) in
  [`invites-plan.md`](../archive/meso/invites-plan.md).
- 2026-06-29 — **N4 — athlete onboarding / invites — Phase 2 built** (branch
  `meso-invites-phase2`, **no migration**). Closes the bidirectional half the relationship spine
  always supported in the model (`CoachAthlete.request` → `pending_athlete_request`) but never in
  the UI: an athlete who already has an account asks to train under a coach, the coach
  accepts/declines on the roster, and either party sees the pending state on their own surface.
  New `CoachAthlete.initiator()` (mirror of `recipient()` — who may *withdraw* a pending link).
  **`athlete_request_coach`** (`POST /meso/request/`): resolves the posted email to a *coach* (a
  `User` with a `CoachProfile`, excluding self), rejecting unknown/non-coach/own; an already-active
  link is untouched, an already-pending request (or coach-invite already awaiting the athlete) is a
  friendly no-op, else `request()` opens/reopens; emails the coach best-effort on
  `transaction.on_commit`. **`request_withdraw`** (`POST /meso/request/<token>/withdraw/`):
  initiator-only (recipient/stranger → 403), pending-only → declined. The coach's accept/decline
  rides the **existing** `invite_accept`/`invite_decline` recipient views unchanged (a request's
  recipient *is* the coach). `notifications.send_coach_request_email` (+ 3 templates), mirror of the
  invite email. Surfaces: the roster gains a pending-request list (Accept/Decline), the athlete home
  gains a "Your coaches" card (incoming invites + sent requests + a request-a-coach form).
  **Routing change:** `RosterView` now sends *any* non-coach to `/meso/me/` (coach = `CoachProfile`
  **or** a coach-side link **or** a sent invite), so a brand-new athlete (or one merely awaiting an
  invite) reaches the request form instead of an empty coach roster. Seeded a demo pending request
  (`hopeful@example.com`) so the surface shows on a fresh DB (idempotent + torn down). Built
  red→green: **+34 pytest** (`test_requests.py`) + 3 seed assertions; full suite green (867).
  **Codex review loop CLEAN on iteration 1.** Plan + deferred (resend/expiry, stub-athlete,
  attribution) in [`invites-plan.md`](../archive/meso/invites-plan.md).
- 2026-06-29 — **N4 — invites — Phase 3 built** (branch `meso-invites-phase3`). Invite
  *lifecycle*: a TTL + an explicit resend, closing the top deferred item. **One migration**
  (`0016_coachinvite_expiry`): `CoachInvite.expires_at` + a new `Status.EXPIRED`. A fresh invite
  is stamped `now + INVITE_TTL` (14 days) by `open_for`; a **null** clock = never expires (legacy
  rows stay claimable — data-safe, no backfill). `is_claimable = is_pending and not is_expired` is
  the single gate the claim path enforces. `expire()` (`pending → expired`, past-due only) fires
  **lazily** (the claim view ages out an overdue link on view; `accept()` flips + refuses one as a
  backstop, so a stale token can never materialize a link) and in **bulk** via a new
  `meso_expire_invites` management command (cron-friendly sweep of `overdue()`, `--dry-run`).
  `resend()` re-arms an outstanding invite — **new token** (old emailed link dies — the Phase-3
  decision), reset clock, `expired → pending`; `open_for` reuses an outstanding (pending/expired)
  row and re-arms a stale one (no duplicate pending+expired pair); `revoke()` broadened so a coach
  can dismiss an *expired* invite too. New `POST /meso/invite/<token>/resend/`
  (`coach_invite_resend`): coach-scoped (404), row-locked, best-effort email on `on_commit`,
  answered-invite no-op (no 500). Querysets `claimable`/`overdue`/`outstanding`; the roster lists
  `outstanding()` (an expired one reads "Expired" + offers Resend); the claim page gains an
  "expired" state; admin lists `expires_at`; the demo invite seeds via `open_for` (real TTL).
  Built red→green: **+38 pytest** (`test_invite_lifecycle.py`); full project suite 904 + 83 Vitest
  green. Plan + deferred (configurable TTL, expiry reminder, cron scheduling, stub-athlete) in
  [`invites-plan.md`](../archive/meso/invites-plan.md).
- 2026-06-29 — **Agent job → django-q `async_task` built** (branch `meso-agent-django-q`,
  **no migration**). Closes the top deferred item of the scheduling plan: `meso/agent/jobs.py` ran
  the proposal job on a bare daemon thread because there was no queue; now that django-q2 + the
  `qcluster` exist (the invite sweeps' scheduler), the agent job rides that same cluster.
  `dispatch_proposal` enqueues `run_proposal_job` (the unchanged unit of work) via `async_task`
  **on commit** — so a worker in another process never races the not-yet-committed drafting batch,
  and a rolled-back request enqueues nothing. **Only the batch id is enqueued**: the worker is a
  separate process that rebuilds its own Claude client (`get_default_client` off the shared `.env`
  `ANTHROPIC_API_KEY`), and a client isn't picklable. The dotted path lives in one constant
  (`RUN_PROPOSAL_TASK`) covered by an end-to-end test that runs the enqueued job under django-q's
  `sync` mode (catches a rename that would break dispatch silently). `MESO_AGENT_RUN_SYNC` still runs
  the job inline (tests + any queue-free env); a broker-write failure resolves the batch to `failed`
  rather than stranding it `drafting` (mirrors the service's "never leave a batch stuck drafting"
  invariant). No compose change — the `qcluster` already runs and shares web's image + `.env`. Built
  red→green: **+3 pytest** (`test_agent_jobs.py` `TestDispatch`, net; the daemon-thread test
  retired). Plan + remaining deferred in [`scheduling-plan.md`](./scheduling-plan.md).
- 2026-06-29 — **"What's next?" YAGNI review + delivery-email opt-out built.** With the whole
  Meso area shipped and no obvious next big slice, each remaining candidate was reviewed
  one-by-one (a skeptical agent per candidate, grounded in the real code) to decide *build now* vs
  YAGNI. **Verdicts:**
  - **S4 (results ↔ records) — skip.** The "results screen shows a PR" premise was mock-only (the
    real results screen never showed a PR; the lone PR string lived in a retired roster mock), and
    `challenges.Record` is a `DurationField`/time-based model — wrong domain for a strength PR.
    Meso already owns best-lift data (`AthleteOneRm`). Embedded modeling question resolves for
    free: **keep separate**; build a PR surface only on a concrete need.
  - **S5 (real-time transport) — skip.** The agent emits one atomic tool-call result behind the
    human review gate (no partial output to stream), the status poll is cheap + bounded (1.5s,
    ≤40 attempts), and the stack is pure WSGI. SSE/websockets would pull in ASGI + channels + a
    Redis channel layer on a `noeviction` box for a ~1.5s cosmetic win. Revisit only if the agent
    becomes genuinely streaming.
  - **Deferred follow-ups — 5 of 6 skip:** cross-unit 1RM (no in-app way to change a plan's unit),
    1RM `set_by` attribution (no consumer), push re-deliver debounce (already mitigated by the
    push `tag` collapse), invite configurable-TTL (one-line constant), invite stub-athlete (a
    *deliberately rejected* design — `decisions.md` N4). **The 1 that made the cut → built (below).**
  - **S6 (billing) — re-added to the plan, NOT skipped.** The review found Meso is single-operator
    *today* (no self-serve coach signup, no subscription infra), but the owner's direction is that
    Meso is **not** to be planned as a single-operator tool — multi-coach SaaS with paid coaches is
    intended. So billing is promoted to the **next major slice** (suggested-sequence item 4); it
    needs the pricing/packaging product decisions first, then an additive Stripe-subscription
    build. (A `billing-plan.md` should capture those decisions before code.)
- 2026-06-29 — **Delivery-email opt-out built** (branch `meso-email-unsubscribe`, migration
  `0019_athleteprofile_delivery_email_opt_out`). The one follow-up that made the YAGNI cut: the
  delivered-week email — the single transactional message a coached athlete receives — had **no
  off switch** (web push is opt-in via the browser permission; email was not). Built the email
  best-practice: a working, login-free, RFC 8058 **one-click `List-Unsubscribe`**. New
  `meso/unsubscribe.py` mints a signed token (`django.core.signing`, no token column, no expiry)
  naming the athlete; `send_week_delivered_email` (switched `send_mail` → `EmailMultiAlternatives`)
  sets `List-Unsubscribe` + `List-Unsubscribe-Post` headers and a visible footer link when given an
  `unsubscribe_url`. The `unsubscribe_delivery_email` view (`/meso/unsubscribe/<token>/`,
  `@csrf_exempt`, login-free) **never mutates on GET** (scanners/prefetchers issue GETs → a confirm
  page); POST (one-click or the human form) flips a single **`AthleteProfile.delivery_email_opt_out`**
  flag. The deliver hook (`_notify_athlete_delivered`) gates the email on the flag — an opted-out
  athlete is emailed nothing, but **push still fires** (separate channel) and delivery still
  succeeds. Intentionally *not* a notification-preferences system — one flag for the one email that
  needed an off switch. Admin surfaces + filters the flag. Built red→green: **+16 pytest**
  (`test_unsubscribe.py`); ruff + format + `makemigrations --check` clean.
- 2026-06-29 — **First-time UX / onboarding slice planned** (not built; plan in
  [`first-time-ux-plan.md`](../archive/meso/first-time-ux-plan.md)). The feature area is broad
  and deployed but has never had an onboarding pass. The plan covers all three
  first-timers (cold visitor · new coach · new athlete) and surfaces the
  **headline blocker**: a coach **cannot create an individual program in the UI** —
  `Plan.objects.create` lives only in `MesoGroup.create_shared_plan`
  (`models.py:1488`), there's no individual-plan / add-week / add-session endpoint,
  and both "+ New program" and "Build a program" CTAs bounce off the bare designer
  back to the roster; only `seed_meso_demo` builds an individual plan tree. Phased
  fix (Phase 1 = individual plan creation, the structural fix; 2–5 = front
  door, empty states, role fork, athlete + designer first-run polish). Reconciled
  with the post-#311 routing (`RosterView` now sends any non-coach to `/meso/me/`,
  so the new-coach gap is now *reaching the coach surface at all*; #311's
  athlete→coach **request** loop already covers most of Phase 4's athlete-initiated
  item — distinct from Q4's "become a coach / beta access").
  **Decisions Q1–Q4 resolved (2026-06-29):** **Q1** coaches = **closed beta /
  allowlisted** (`CoachProfile` auto-creates on first coach action for an
  allowlisted user; open self-serve deferred to billing S6 — no billing yet + a
  per-coach Claude cost + a single box make open signup premature; the YAGNI-review
  entry above promotes **billing/S6 to the next major slice**, which is exactly when
  open coach self-serve becomes viable); **Q2** plan creation = **blank editable
  scaffold first**, optional agent draft as a fast follow (template library
  deferred); **Q3** = **yes** to a removable one-click demo (no demo-athlete
  email/push); **Q4** = a **logged-out `/meso/` landing** with "I have an invite" +
  "Request coach access" + one main-site link (not instant-signup). Q1 is the most
  consequential and the easiest to revisit.
- 2026-06-29 — **S6 billing — decisions locked + plan written** (no code yet; planning only).
  Worked through the billing decision set with the owner. **Locked:** D1 the **coach** pays (B2B);
  D2 **per-active-athlete (seat) pricing** (a seat = an active `CoachAthlete` link); D3 **free tier
  + a 14-day no-card trial** (the trial is **local state** — no Stripe until a card is actually
  collected — and the free tier is the lapse/cancel landing spot); D4 the paywall gates **athlete
  count + the AI agent** (the Claude agent has real per-call cost → free tier gets no agent;
  trial/paid/comped get it; groups + notifications stay free; both gates share the `is_active`
  predicate); D5 **monthly/USD** (annual
  deferred); D6 Stripe Smart Retries then **downgrade to free at period end**, over-limit blocks
  new athletes + edits but **never deletes**; D11 first slice = the **subscription spine** for
  existing coaches (public self-serve coach signup is a later phase). **Recommended architecture
  (proceed unless overridden):** Stripe Billing + subscription Checkout + the hosted **Customer
  Portal**; Stripe is source-of-truth with a thin local **`CoachSubscription`** mirror (1:1 → coach
  `User`) for fast gating; a **separate clean billing webhook** (the messy products webhook is left
  alone); one `billing/access.py` accessor (`can_add_athlete`); seat-quantity sync best-effort +
  a daily **`reconcile_seats` qcluster sweep** (reuses the django-q2 cluster); a **`comped`** status
  so the owner + demo coaches are never paywalled. **5-phase build** in
  [`billing-plan.md`](./billing-plan.md) — Phase 1 (this slice) = `CoachSubscription` model +
  migration + the gating accessor + local trial + comped seed/admin, **no Stripe, no enforcement
  wired** (same state-machine-first order as the invite slice). **Open values** (not architecture):
  free seat count (rec 1), per-seat price (TBD — owner's number), trial 14d.
- 2026-06-29 — **First-time UX — Phase 1 built & merged** (PR #326): individual
  plan creation, the headline structural fix (`Plan.scaffold` + `CoachAthlete.create_plan` /
  `working_plan` + `plan_create` / `session_add` endpoints + wired CTAs). A real coach can now
  build an individual program in the UI with no seed. Plan in
  [`first-time-ux-plan.md`](../archive/meso/first-time-ux-plan.md).
- 2026-06-29 — **First-time UX — Phase 2 built** (branch `meso-first-time-ux-phase2`): **coach
  first-run — one-click demo + empty-state teaching** (Q3). `meso/demo.py`
  (`load_demo` / `clear_demo` / `has_demo`) is a coach-scoped, idempotent wrapper over the
  `seed_meso_demo` data that stands up five demo athletes + a built/delivered/logged individual
  program + a group (shared program + a couple of per-athlete overrides), **namespaced per coach**
  (non-routable `@<coach-hex>.demo.invalid` addresses, collision-free across coaches). New
  **`is_demo`** flag on `CoachAthlete` + `MesoGroup` (migration `0022`) makes demo data **clearly
  labeled** (roster banner + per-row "Demo" badge), **fully removable** (`clear_demo` deletes the
  demo group + demo athlete users, cascading their links/plans/logs/memberships/overrides), and
  **billing-neutral** — a new `CoachAthlete.billable()` (= `active().exclude(is_demo=True)`) backs
  `access.active_seat_count` / `suspended_athlete_ids` so loading the demo never trips the paywall
  or suspends real athletes. **No demo-athlete email/push:** the load delivers at the model layer
  (no notification), the addresses are non-routable, and each carries `delivery_email_opt_out`.
  `POST /meso/demo/{load,clear}/`; the empty roster gets a first-run onboarding card (teach
  Invite → Build → Deliver + the demo CTA); `_meso_base.html` now renders flashed messages
  (previously swallowed on every meso page). **Q1 dropped** — its "allowlisted-coach access"
  premise (*no billing yet*) was obsoleted by billing S6 Phase 4 (#323, open self-serve coach
  signup), so the closed-beta allowlist was **not** built (it would contradict shipped behavior).
  Built red→green: **+23 pytest** (`test_demo.py`); ruff + `makemigrations --check` clean.
  Resume point → first-time-UX **Phase 3** (anon `/meso/` landing + main-site link).
- 2026-06-29 — **First-time UX — Phase 3 built** (branch `meso-first-time-ux-phase3`,
  PR #329, **no migration**): **the front door** (anonymous visitor +
  discoverability). `/meso/` was login-gated (`RosterView(LoginRequiredMixin)`), so a
  cold visitor met a bare login wall and Meso was linked from **nowhere** on the main
  site. `RosterView` now **splits on auth**: an anonymous visitor renders the new
  login-free `meso/landing.html` (what Meso is + two honest entry actions — *log in as
  an athlete*, carrying `?next=` back to `/meso/me/`, and *become a coach* via the
  #323 funnel) instead of bouncing to `/accounts/login/`; an authenticated visitor
  keeps the post-#311 role routing (coach → roster, anyone else → `/meso/me/`)
  untouched (the authenticated branches read `request.user` only after the anonymous
  one returns). A discreet **"Coaching"** link in the main-site nav (`_nav.html`)
  makes Meso discoverable without already knowing the URL. **Q1's closed-beta
  "Request coach access" was not built** — obsoleted by #323's open self-serve signup
  (same reconciliation as Phase 2); the coach entry action is the plain *become a
  coach* path, and the **become-a-coach-from-athlete-home** item already shipped in
  #323 (`athlete_home.html`'s "Are you a coach?" card), so Phase 3 narrowed to the
  anon landing + the main-site link. Built red→green: **+11 pytest**
  (`test_landing.py`) + the stale `test_roster_requires_login` repurposed to
  `test_anonymous_sees_landing_not_login`; full project suite green, ruff + format +
  `makemigrations --check` clean. **Codex review loop CLEAN on iteration 1.** Resume
  point → first-time-UX **Phase 4** (athlete install/first-log polish) **or Phase 5**
  (designer/agent self-explanation). Plan in
  [`first-time-ux-plan.md`](../archive/meso/first-time-ux-plan.md).
- 2026-06-30 — **First-time UX — Phase 4 built & merged** (branch
  `meso-first-time-ux-phase4`, PR #330, **no migration**): **athlete first-run
  polish** — a PWA **install prompt** + a one-time **first-log coachmark**. The
  coachmark is **server-driven**: `views._athlete_has_completed_log(user)` gates it
  on the home (only when a delivered session exists to tap) and the session logger,
  so it's naturally one-time + cross-device and vanishes the moment the first
  *completed* log lands — **no per-device flag, no migration**. Gated on a `done`
  log specifically (a "Save progress" `pending` draft must not retract the hint that
  teaches the final "Log session" step). New **`meso_onboarding.js`** reveals the
  install card from a captured `beforeinstallprompt` (Chromium), falls back to manual
  *Add to Home Screen* steps on iOS (incl. **iPadOS-13+ Safari's desktop "Macintosh"
  UA** via `detectIOS(ua, maxTouchPoints)`), stays hidden when already
  installed/dismissed, and persists manual coachmark dismissals. Visibility toggles
  via inline `style.display`, **not** the `hidden` attribute — an inline `display:flex`
  carried for layout beats the UA `[hidden]` rule (the existing push CTA has this
  latent bug, masked only because push is unconfigured in prod). The script is added
  to the SW **PRECACHE** (cache bumped **`v1`→`v2`**) so it works offline like the
  other athlete scripts. Built red→green: **+12 pytest** (`test_athlete_onboarding.py`
  + a precache guard in `test_athlete_pwa.py`) + **+13 vitest**
  (`meso_onboarding.test.js`); 1035 meso pytest + 99 vitest green. **Codex review loop
  CLEAN after 3 fix iterations** (iPadOS UA detection → done-log gating → SW precache).
  **Prod-verified:** `/meso/sw.js` now serves `CACHE = "meso-pwa-v2"` + the hashed
  `meso_onboarding.*.js` (HTTP 200). Resume point → first-time-UX **Phase 5**
  (designer/agent self-explanation) **or** the add-week/week-switcher deferral. Plan in
  [`first-time-ux-plan.md`](../archive/meso/first-time-ux-plan.md).
- 2026-06-30 — **First-time UX — Phase 5 built** (branch `meso-first-time-ux-phase5`,
  **no migration**): **designer & agent self-explanation** — the **last first-time-UX
  phase**. The designer is a self-contained Alpine page that shipped a pile of
  *prototype chrome*: a hardcoded fake athlete (a stock name + invented
  contraindications), a fabricated "Coach's programming style" block, and a hardcoded
  macrocycle — **all rendered over whatever real plan the coach opened**. A first-time
  coach also got no orientation: nothing said the grid autosaves, that the agent only
  *proposes* (changes wait at the review gate), or that the phone column is the
  athlete's real view. Three parts: **(1) Coachmarks** — three **dismissible** first-run
  notes anchor the designer's regions (week grid · agent · phone preview); they show
  until dismissed, the dismissal persisting client-side in `localStorage` (`meso.js`,
  namespaced `meso-coachmark-designer-<key>` so it never collides with the athlete
  onboarding coachmarks' `meso-coachmark-` prefix) — **no server "seen" flag**, like
  the athlete chrome. **(2) Agent self-explanation** — a **persistent** "propose →
  review → apply" note under the agent header makes the review gate explicit for
  *everyone* (not just first-timers — a newcomer won't expect the agent to only
  propose); individual-only (the group agent's composer is hidden). **(3) Real chrome**
  — `serialize_plan` now carries the individual plan's **real athlete identity**
  (`serialize_athlete_identity`: name / initials / goal / **active** contraindications,
  the same global injuries the agent grounds on), and the left-rail athlete card,
  macrocycle rail, top-bar identity/chip, and week/block headers render the real
  `athlete`/`weeks`/`phases` (new `meso.js` getters `currentWeek`/`currentPhase`/
  `cycleLabel`/`weekHeading`/`blockHeading`). The invented coach-preferences block was
  **removed** as misleading; group mode (already real, via `group`) is untouched —
  `serialize_athlete_identity` returns `None` for a group plan. Built red→green: the
  dismiss logic is unit-tested in `frontend/meso.test.js` (**+5 vitest**), the server
  seam (serializer identity, rendered coachmarks + note, absence of the fabricated
  chrome, `meso.js` dismiss API) in `test_designer_onboarding.py` (**+10 pytest**);
  1055 meso pytest + 104 vitest green, ruff + format clean, DjHTML clean. **Gotcha
  (cost me 2 cycles):** the render tests assert the fabricated strings are *gone*
  (`"Maya"`, `"programming style"` ∉ body) — my own explanatory template comments
  mentioning those strings tripped the asserts, so dev-facing comments must avoid the
  very tokens the tests forbid. **Codex review loop CLEAN on iteration 1.** This
  **completes the first-time-UX slice** (Phases 1–5). Remaining Meso backlog: the
  **add-week / week-switcher** deferral (designer is single-current-week) and **S6
  billing Phase 5 annual prices** (blocked on the owner's per-seat number + a Stripe
  annual Price). Plan in [`first-time-ux-plan.md`](../archive/meso/first-time-ux-plan.md).
- 2026-06-29 — **Multi-week designer built** (branch `meso-multi-week-designer`,
  **no migration**): closes the long-standing **add-week / week-switcher** deferral.
  A plan was effectively **single-week** — `Plan.scaffold` materialized one `Week`
  (`is_current`) and the only growth verb was `session_add` (a day in *that* week),
  so a coach could not build a multi-week mesocycle, review an earlier week, or aim
  delivery anywhere but the scaffold's first week. Three model/seam pieces +
  three endpoints + a designer strip: **(1)** `Mesocycle.append_week()` materializes
  the next week by **copying the latest week's grid** (sessions + prescriptions — a
  real progression starting point, loads carried forward for the coach to tweak, not
  a blank — **and, for a group shared plan, each member's `PrescriptionOverride`** so
  a per-athlete swap/load-cut isn't silently dropped on the new week's delivery);
  the new week is a **non-current, undelivered draft** (adding it never changes
  what's live or deliverable), and `week_count` grows to track the highest
  materialized index. **(2)** `serialize_week` gains `id`/`index` and `serialize_plan`
  gains **`viewing`** (the open week's pk) so the client can tell the *viewed* week
  apart from the *current* (deliver-target) one. **(3)** three endpoints under
  `api/plan/<id>/week/…` — `GET week/<id>/` **views** any week (a pure read, scoped by
  ownership only, **not** billing-gated, so a suspended coach keeps read access);
  `POST week/` **adds** the next week (edit-gated, mesocycle row-locked against the
  `unique_week_index` race); `POST week/<id>/current/` **sets the live/deliver-target**
  week (edit-gated, plan row-locked, clears the other weeks). The designer renders a
  **week-switcher strip** in the week view (chips → `switchWeek`, a live-week dot,
  "+ Add week" → `addWeek`, and a "Make current" → `setCurrentWeek` shown only when the
  viewed week isn't live) and the periodization timeline bars are now click-to-view;
  `meso.js` tracks `viewedWeekId`, and a shared `applyPlanData` keeps program / week
  strip / phases / viewed pointer in lockstep across init + the three verbs; the
  week/cycle **headers follow the viewed week**. **Design call:** *viewing is a pure
  read; making a week the deliver target is the separate explicit `set-current`
  action* — so building weeks ahead never silently moves what delivery sends, and
  reviewing a past week never re-marks it live (no footgun). Deliver code is
  **unchanged** (still sends `current_week`, now coach-controllable). A Codex review
  caught two correctness gaps that were fixed in-PR: `session_add` ("+ Add day") still
  targeted `current_week`, so adding a day while viewing a non-current week landed it
  on the wrong week — now an optional `week_id` pins the **viewed** week; and
  `append_week` originally dropped group overrides (above). Built red→green:
  `test_week_management.py` (**+37 pytest**: `append_week` incl. override carry-forward,
  the three endpoints incl. scoping/402/404/405, `session_add` week-scoping, serializer
  shape, a group variant, render/JS wiring) + `frontend/meso.test.js` (**+9 vitest**:
  `applyPlanData`, the viewed-week getters, `switchWeek`/`addWeek`/`setCurrentWeek`
  incl. no-op + failure paths) + the `addDay` test now asserts the week scope; updated
  the exact-shape serializer test for `id`/`index`/`viewing`. 1078 meso pytest + 112
  vitest green, ruff + format clean. Remaining Meso backlog: **deliver a chosen non-current
  week without first making it current** (a natural follow-up) and **S6 billing Phase
  5 annual prices** (blocked on the owner's per-seat number + a Stripe annual Price).
- 2026-06-30 — **Deliver any week built** (branch `meso-deliver-any-week`,
  **no migration**): closes the multi-week designer's follow-up — **deliver a
  chosen, non-current week without first making it current**. After the
  multi-week designer, a coach could build weeks ahead and switch which is live,
  but `plan_deliver` only ever sent `current_week`, so sending a built-ahead week
  meant first flipping it current (moving the live pointer as a side effect).
  Now: **(1)** `plan_deliver` takes an optional `week_id` (individual plans) and
  delivers *that* week — stamp `delivered_at` + `WeekDelivery` snapshot —
  **without touching `is_current`**; a foreign week is a 404, absent → the live
  week (unchanged), and a **group plan ignores `week_id`** (still fans out its
  current week — per-week delivery is an individual-designer affordance). **(2)**
  `deliver_screen(plan, week=None)` targets a chosen week, lists every week for a
  selector, and flags whether the target is the live week. **(3)** `DeliverView`
  resolves `?week=` (foreign / non-numeric → silently falls back to live; the
  confirm screen always renders something deliverable, the POST validates
  strictly). The deliver screen gains a **per-week selector** (chips link
  `?week=<id>`, server-rendered so the summary stays consistent) + a **"not the
  live week" notice**; the designer's "Deliver" link carries the **viewed** week
  (`:href="deliverHref"` → `?week=<viewedWeekId>`) so "Deliver" sends the week on
  screen. **Design call:** *delivery never moves the live pointer* — visibility
  stays "newest delivery wins" (`latest_delivered_week`), so the athlete lands on
  the week just sent while the coach's `is_current`/deliver-default holds (the
  multi-week "no footgun" stance, extended to delivery). Built red→green:
  `test_deliver.py` (**+~20 pytest**: per-week deliver + leaves-current,
  athlete-visible week, foreign-404, over-limit 402, screen `?week=` targeting /
  selector / notice / fallback) + `test_group_deliver.py` (**+1**: group ignores
  `week_id`) + `frontend/meso.test.js` (**+3 vitest**: `deliverHref`). 1097 meso
  pytest + 54 vitest green; ruff + format + djhtml + `makemigrations --check`
  clean. **Codex review loop:** 4 fix iterations then CLEAN — all four were the
  same class of **strict-input** nit on the new JSON `week_id` path (P2 non-int pk
  → 500 → coerce; P2 malformed-body → silent live-week delivery → 400, gated on
  `content_type == "application/json"` so bodyless/multipart callers still mean
  "live week"; **shared `_body_week_id` helper** wired into `plan_deliver` *and*
  the adjacent pre-existing `session_add`, which carried the identical latent
  vector; P3 the live-week warning read the raw `is_current` flag, contradicting
  `current_week`'s earliest-week fallback → resolve one `live_id` for both the
  chip marker and the notice; P2 `int()` coerced `1.9`→1 / `True`→1 → accept only
  a genuine JSON integer, `bool` excluded). **GOTCHA (reusable):** Django's test
  client `client.post(url)` with **no data still sends a non-empty multipart
  body** (boundary bytes), so a `if not request.body` guard won't see it as empty
  — gate strict JSON parsing on `request.content_type` instead. Plus the
  recurring render-test gotcha (a `// live week` comment in the deliver
  `<script>` tripped the "no 'live week' on the current screen" assert — scrub
  dev-facing comments of asserted tokens). Remaining Meso backlog: **S6 billing
  Phase 5 annual prices** (blocked on the owner's per-seat number + a Stripe
  annual Price) — no other autonomous slice outstanding.
- 2026-06-30 — **First-time UX — Q2 fast-follow: agent-drafted starter plan
  built, merged & deployed** (PR #335, squash `f94d48c`, migration `0023`):
  closes the first-time-UX **Q2** fast-follow ("blank scaffold first, optional
  agent draft as a fast follow"). When a coach creates a new individual program,
  a **"Draft with AI"** CTA hands the freshly-created scaffold to the agent to
  draft the first week; the proposal lands in the **existing review gate** (no
  auto-apply). The agent previously only *edited* existing rows
  (swap/progress/volume/deload), so it couldn't build onto a bare scaffold — this
  adds an **`add`** verb (`ProposedChange.Kind.ADD`, migration `0023`, a
  schemaless choices alter): the client tool gains the `add` kind +
  `new_reps`/`new_rpe` fields + prompt guidance; `agent/validation.py` validates
  an `add` (targets a *session* in the current week, builds the new row, requires
  a name, and the **contraindication backstop screens the introduced movement**
  exactly like a swap); `agent/apply.py` `_apply_add` creates the prescription on
  the target session, ordered after existing rows. **Draft kickoff:**
  `plan_create` takes a `draft` flag — it only fires on a **freshly-created** plan
  (never overwrites an existing program), is **metered like the manual agent run**
  (coach-row-locked `can_use_agent` reservation; the `AgentProposalBatch` table is
  the run ledger), creates a `drafting` batch with the canned
  `agent_service.DRAFT_INSTRUCTION`, and dispatches the job; the coach lands in
  the designer where the persisted chat thread shows the draft resolve with a
  "Review N changes →" link. Degrades to a **blank plan + a flash** when the
  allowance is exhausted or no API key is configured. **UI:** "Draft with AI"
  buttons on the athlete-profile first-program CTA and the roster "+ New program"
  disclosure, gated on the agent allowance (and, on the roster, hidden for an
  athlete who already has a plan — where the draft would be a no-op, the Codex P3
  fix). Built red→green: **+48 pytest** (`test_plan_draft.py` + add-kind coverage
  in `test_agent_validation.py`/`test_agent_apply.py`); 1263 project pytest + 115
  vitest green, ruff + DjHTML + `makemigrations --check` clean. **Codex review
  loop CLEAN after 1 fix iteration** (the roster no-op CTA). Prod-verified: deploy
  succeeded (migration `0023` applied), `/meso/` serves 200 after restart.
  Remaining Meso backlog unchanged: **S6 billing Phase 5 annual prices** (blocked
  on the owner's per-seat number + a Stripe annual Price) — no other autonomous
  slice outstanding.
- 2026-06-30 — **Changes-since-last-delivery diff UI built, merged & deployed**
  (PR #337, squash `3224d30`, **no migration**): ships the long-deferred **full
  diff UI** (persistence-plan open assumption #3, cited across the
  persistence/agent/athlete/first-time-UX plans). Delivering a week always
  recorded a `WeekDelivery` snapshot (`serialize_week_snapshot`), but nothing
  read it back — the deliver screen's "Changes since last delivery" card just
  said "re-delivering with your latest edits". Now, on a **re-delivery**, the
  deliver confirm screen diffs the **target** week's live grid against the
  snapshot last delivered so the coach reviews exactly what's about to change for
  the athlete. Three seams: **(1)** `serializers.diff_week_snapshots(current,
  previous)` — a **pure** diff over two snapshot payloads matched by **stable
  pks** (a row in both with differing fields is *changed*, with per-field
  before/after over name/sets/reps/load/load_type/rpe/note/tag; a new pk is
  *added*, a missing one *removed*; whole sessions added/removed are surfaced
  separately and not double-counted as row diffs; week-meta
  phase/volume/intensity/deload diffed too). Returns `None` when there's no prior
  payload; `has_changes` is `False` when the week is unchanged since its last
  delivery. **(2)** `presenters.deliver_screen` computes `deliver["changes"]`
  (the last `WeekDelivery.payload` for the **target** week vs its live snapshot)
  — `None` on a first delivery; because it keys on the *target* week, a
  built-ahead week diffs against *its own* last delivery. **(3)** `deliver.html`
  renders first-delivery / no-changes / the grouped diff, styled with the
  existing tokens (`var(--ok)` add, `var(--warn)` remove). **No model change, no
  migration; no JS — fully server-rendered.** Built red→green: **+17 pytest**
  (`test_delivery_diff.py` — pure diff, presenter context incl. chosen-week
  targeting, screen render). 1280 project pytest green; ruff + format + DjHTML +
  `makemigrations --check` clean. **Codex review loop CLEAN after 1 fix
  iteration** — a P2: Django's `default` filter treats a valid `0`
  (volume/intensity) as falsy and rendered the em-dash, fixed with
  `default_if_none` for the week-meta line (prescription string fields keep
  `default`, where blank → dash is intended) + a regression test. **Prod-verified:**
  deploy succeeded (no migration), `https://mastering.fitness/meso/` serves 200
  after restart. Remaining Meso backlog unchanged: **S6 billing Phase 5 annual
  prices** (blocked on the owner's per-seat number + a Stripe annual Price) — no
  other autonomous slice outstanding.
- 2026-06-30 — **Billing price + structure decided (D13): base + per-seat,
  TrainHeroic-style.** The long-open "per-seat price TBD" is settled: **$9.99/mo
  flat base + $1/mo per active seat**, USD — mirroring TrainHeroic's direct-pay
  Coach Plan. This changes the billing *shape* from a single per-seat Price to a
  **two-line-item** subscription (flat base + per-seat quantity), so it's a code
  slice, not just config: **Phase 6** in [`billing-plan.md`](./billing-plan.md)
  (`stripe_gateway.subscribe` two line items, a nullable `stripe_base_item_id`
  model field + small migration, seat-sync/`reconcile_seats` adjusting only the
  seat item, paywall copy "$9.99/mo + $1/athlete"; annual prices ride along once
  the annual numbers are set). Buildable autonomously red→green (mock the `stripe`
  SDK — no live Stripe access needed); ships **dormant** until the owner creates
  both Prices (`MESO_BASE_PRICE_ID` + `MESO_SEAT_PRICE_ID`) and registers the
  webhook (`MESO_STRIPE_WEBHOOK_SECRET`). **Phase 6 is now the next Meso slice**
  (recorded per the owner — not building yet). **Secret-handling note:** Stripe
  Price ids are not secrets (fine to share); the webhook signing secret + the
  Stripe secret key go straight into the prod env, never through chat / the agent.
- 2026-06-30 — **Agent usage & cost tracking planned** (owner: "we'll need to
  track real usage … based on the client and the coach"). **Launch stays at
  $1/seat;** this is the instrumentation that validates that margin. Anthropic's
  invoice has no coach/athlete attribution, so we capture the `response.usage`
  block ourselves at the call site. Key insight: **`AgentProposalBatch` is
  already the per-run ledger** (carries `coach`, `plan`→athlete-or-group,
  `model`, `status`) — the gap is just **token usage + cost**, so the plan
  **extends the batch** (not a new model for v1) with input/output/cache tokens,
  `request_id`, `stop_reason`, `duration_ms`, computed `estimated_cost_usd` (from
  a per-model rate table in `meso/billing/agent_costs.py`; estimate, invoice is
  truth), plus slicing dims `trigger` (manual/draft/eval/group — excludes evals)
  and a **`billing_status` snapshot at run time** (COGS-vs-CAC split). "Client" =
  the **athlete/seat** (group runs → group, athlete null); the **model** is also
  a first-class dimension (the cost driver). Captured on **failed** runs too
  (mid-stream output still bills). Full plan + phasing (capture → report →
  deferred dashboard) in [`agent-usage-plan.md`](./agent-usage-plan.md); it's the
  decision input for the two billing pressure valves (drop `MESO_AGENT_MODEL`
  tier / meter paid runs). Autonomous, no Stripe — can ship before go-live.
- 2026-06-30 — **Billing Phase 6 built: base + per-seat (TrainHeroic-style, D13).**
  Converted the single per-seat subscription into a **two-line-item** one. New
  `MESO_BASE_PRICE_ID` setting (the $9.99/mo flat base, quantity 1) billed
  alongside the existing `MESO_SEAT_PRICE_ID` (per-seat, quantity = active seats).
  `stripe_gateway` Checkout now emits both line items (base before seat; base
  included only when its Price id is configured — a defensive seat-only fallback).
  Model gained a nullable **`stripe_base_item_id`** (migration `0024`) so seat-sync
  targets only the *seat* item — the base line is fixed at 1 and never resized
  (`sync_seat_quantity`/`reconcile_seats` unchanged: they already key off
  `stripe_item_id` = the seat item). The webhook now **classifies** a
  subscription's two items by Price id (`_classify_items` → the seat item drives
  `stripe_item_id` + `quantity`, the base item → `stripe_base_item_id`), with a
  legacy single-line fallback (first non-base item = seat). The subscribe view
  requires **both** Prices configured before opening Checkout (ships dormant, never
  half-charges). Paywall copy centralised in `presenters.PRICE_SUMMARY`
  ("$9.99/mo + $1 per active athlete") → roster billing card + designer upgrade CTA
  + become-a-coach tiers. Free/trial/comped gates unchanged. Red→green, Stripe SDK
  mocked. Ships **dormant** until the owner creates both Prices + registers the
  webhook. **Annual prices** (a `*_ANNUAL` Price per line + a Checkout toggle) are
  the remaining ride-along, still blocked on the annual numbers.
- 2026-06-30 — **Agent usage tracking Phase 1 (capture) built** (migration `0025`,
  PR #344). Extended `AgentProposalBatch` (the per-run ledger) with the token/cost/
  dimension columns: `input_tokens`/`output_tokens`/`cache_creation_input_tokens`/
  `cache_read_input_tokens`/`api_calls`/`request_id`/`stop_reason`/`duration_ms`,
  a computed `estimated_cost_usd` (Decimal, from `meso/billing/agent_costs.py`'s
  per-model rate table — unknown model → `None`, never a wrong $0), plus the
  `trigger` (manual/draft/eval/group) and `billing_status` snapshots. `client.propose`
  now returns a `ProposalResult(data, usage)` carrying the Anthropic `usage` block +
  `_request_id` + `stop_reason`; the service threads it (with the measured
  `duration_ms`) onto the batch. A **failed** run still records model + duration (U5).
  Admin surfaces the columns read-only. No Stripe — independent of go-live.
- 2026-06-30 — **Agent usage tracking Phase 2 (report) built** (no migration). The
  read side of the captured data: `meso/billing/agent_usage_report.py`
  `build_report(start, end)` rolls a calendar month's **non-eval** runs up into
  per-coach **cost vs revenue → margin** (flagging any *paying* coach whose agent
  cost outran their plan — the $1/seat tail risk D13 called out), a per-(coach,
  client) breakdown to surface the heavy seats (a client = the athlete on an
  individual plan, or the **group** on a group plan, athlete null), and roll-ups by
  **model**, **trigger**, and **billing tier** (the COGS-vs-CAC split off each run's
  snapshot `billing_status`: active/past_due = paid, comped = owner/demo, else
  free/trial = CAC). Revenue = the coach's *current* plan price (`$9.99 base + $1 ×
  current billable seats` — `BASE_PRICE_USD`/`SEAT_PRICE_USD` mirror
  `presenters.PRICE_SUMMARY`), an approximation since per-month historical seat
  counts aren't stored. The `meso_agent_usage_report` management command renders it
  (`--month YYYY-MM`, default current; `--json` for machine output). Estimated cost
  stays the internal number; the Anthropic invoice is authoritative. Eval runs are
  excluded everywhere (a quality check, not coach usage) but counted as a footnote.
  Red→green; `test_agent_usage_report.py`. **Deferred** (Phase 3): an owner
  dashboard, a margin-threshold alert, and reconciliation against the Anthropic
  Admin/Usage API.
- 2026-06-30 — **Agent usage tracking Phase 3 (margin alert) built** (migration
  `0026`, schedule-only — no Stripe). The early-warning push on top of Phase 2's
  passive report: instead of the owner remembering to run a report, the app
  proactively emails them when a *paying* coach's estimated agent cost crosses a
  fraction of their plan revenue. `CoachUsage` gained `cost_to_revenue_ratio`
  (`None` at $0 revenue) and `at_risk(threshold)` — a paying coach with
  `cost > threshold × revenue` (strict; it generalizes `flagged`, the
  `threshold == 1` / cost-already-past-revenue case). `agent_usage_report`
  gained `margin_alerts(report, threshold)` (at-risk coaches, worst ratio first)
  and `previous_month_bounds()` (the closed-month window for the cron).
  `notifications/emails.send_margin_alert_email` emails the owner
  (`settings.ADMINS`, from the `SERVER_EMAIL` robot) — operational, not
  customer-facing — only when there are alerts and an admin address. The
  `meso_agent_margin_alert` command (`--month` / `--last-month` / `--threshold` /
  `--dry-run`) builds the month, lists the at-risk coaches, and sends the email
  best-effort (a mail failure is logged, never crashes the sweep). Default
  threshold = `MESO_MARGIN_ALERT_THRESHOLD` (0.5, overridable per run).
  `tasks.agent_margin_alert` wraps the command with `--last-month`; migration
  `0026` registers a **monthly** `django_q.Schedule` (`schedule_type="M"`) at it —
  the first non-daily Meso sweep (a monthly cron summarizes a full closed month,
  not the partial current one). Free/trial coaches never alert ($0 revenue is CAC
  by design — same rule as `flagged`). Red→green: `test_agent_margin_alert.py`
  (pure ratio/at-risk/margin_alerts, previous-month window, owner email, command
  across windows/thresholds/dry-run/validation) + `test_scheduler.py` (monthly
  registration + the task wrapper over the previous month). The two billing
  pressure valves it informs (drop `MESO_AGENT_MODEL` / meter paid runs) stay
  deferred — gated on what the data shows. **Remaining agent-usage backlog:** an
  owner dashboard + Anthropic Admin/Usage-API reconciliation (both deferred);
  remaining Meso backlog otherwise: billing annual prices (blocked on the owner's
  annual numbers) + the group agent (LARGE owner-decision).
- 2026-06-30 — **Agent usage tracking Phase 4 (owner dashboard) built** (no
  migration). The web read-out of the data Phases 1–3 capture/aggregate/alert on,
  closing the deferred dashboard item. `UsageDashboardView` (`/meso/usage/`) is a
  **staff-gated**, all-coach view of `build_report` for a `?month=YYYY-MM` window:
  `UserPassesTestMixin` on `is_staff` bounces an anonymous visitor to login and
  gives an authenticated non-staff coach a flat **403** (`handle_no_permission`),
  so a coach can't probe org-wide spend; a malformed `month` degrades to the
  current month with a flashed warning rather than 500ing. `presenters.usage_dashboard`
  adapts the `Report` into the template context (a `YYYY-MM` label + prev/next
  month nav, the threshold %, the `margin_alerts` subset, the roll-ups pre-sorted
  by cost). Three new **pure, tested** helpers on `agent_usage_report`:
  `shift_month` (prev/next month arithmetic, year-boundary safe), `resolve_alert_threshold`
  (a **never-raising** settings/override resolver, `DEFAULT_ALERT_THRESHOLD` 0.5 —
  the dashboard must render even with a misconfigured `MESO_MARGIN_ALERT_THRESHOLD`,
  so unlike the command's `_threshold` it returns the default rather than erroring),
  and `sorted_totals` (cost-sorted roll-up pairs). `usage_dashboard.html` +
  `_usage_rollup.html` render the totals, a margin-alert banner, the by-tier/model/
  trigger roll-ups, and the per-coach cost-vs-revenue-margin rows with a per-client
  breakdown; an `is_staff`-gated **"Usage"** nav link in `_meso_base.html` (the
  first owner-only meso surface). **No model change, no migration.** Red→green:
  **+25 pytest** (`test_agent_usage_dashboard.py` — pure helpers, presenter, the
  staff gate, month windowing / invalid-month fallback, margin-alert surfacing,
  group attribution); 1263 meso pytest green, ruff + DjHTML + `makemigrations
  --check` clean. **Codex review loop CLEAN on iteration 1.** **Remaining
  agent-usage backlog:** only the Anthropic Admin/Usage-API reconciliation
  (deferred — needs an Admin API key + live org access). Remaining Meso backlog
  otherwise unchanged: billing annual prices (blocked on the owner's annual
  numbers) + the group agent (LARGE owner-decision).
  **Superseded by #613:** The gate is now `is_superuser`; `handle_no_permission` is unchanged, and the nav links plus `/backside/` Dashboards module use the same gate.
- 2026-06-30 — **Group agent Phase 1 built: the AI agent edits the shared program**
  (PR #350, no migration). The proposal agent rejected a group plan with a `400`
  (its grounding dereferenced a single `plan.athlete`); now it grounds on the
  **group** and edits the group's **shared program** behind the same
  propose → review → apply gate. `service.build_context` branches on `plan.is_group`
  (a `_group_context`: members + each one's contraindications + the **folded** set
  across all active members; no single-athlete `recent_logs`); `validation.forbidden_terms`
  folds the contraindication backstop across **every active member** (a swap/add
  unsafe for any one member is rejected — the shared row trains everyone);
  `agent.apply` is **unchanged** (it already writes onto the shared
  `ExercisePrescription`, so every member inherits). `agent_propose` drops the
  `400` and tags a group run `trigger=group` (usage ledger → group, athlete null);
  the review/status/apply endpoints widen `for_coach`→`editable_by` to cover group
  batches (identical set for individual plans — no regression); `presenters.review_changes`
  names the group; a group batch's post-apply link routes to the **designer** (no
  individual deliver screen — group delivery is deliver-to-all). The client adds
  group framing in the *user* turn (cached system prompt unchanged); the designer
  shows the agent composer in Group mode (the stale "later phase" placeholder is
  gone). **Decision:** shared-template editing is the first slice — it reuses the
  entire pipeline (validation/apply/review/usage) and is consistent with the group
  designer; **per-athlete auto-adjust generation (Phase 2)** — the agent emits
  per-member `PrescriptionOverride`s — is the deferred follow-up. Red→green
  (`test_group_agent.py`), Codex CLEAN iter 1, deploy success, prod-verified.
  **Remaining Meso backlog:** billing annual prices (BLOCKED on owner numbers +
  Stripe annual Prices); Anthropic Admin/Usage-API reconciliation (deferred, needs
  Admin key); group agent Phase 2 (per-athlete auto-adjusts).
- 2026-06-30 — **Group agent Phase 2 built: the agent proposes per-athlete
  AUTO-ADJUSTS** (migration `meso.0027`). Phase 1 let the group agent edit the
  *shared* program (every member inherits); this slice adds the other half — the
  agent can propose a per-member `PrescriptionOverride` (a swap, a load %, or a
  volume tweak that diverges **one** member from the shared base) behind the same
  propose → review → apply gate. Both coexist: the agent picks a shared edit (whole
  group) or an adjust (personalize one member) from the instruction. **A new agent
  verb `adjust` (`ProposedChange.Kind.ADJUST`)** — the first kind that edits neither
  the shared row nor a new row; `ProposedChange` gains a nullable `membership` FK
  (`SET_NULL`, like `session`/`prescription` → a member removed between propose and
  apply is a safe no-op skip). The `adjust` targets the member by **`member_id`**
  (the `GroupMembership` pk `service._group_context` now exposes per member) + the
  shared `prescription_id`; the tool gains adjust-only `member_id` + `load_pct` (int
  percentage, 90 = −10%), reusing `new_name`/`new_sets`/`new_reps` for the rest.
  **Per-member safety is the key property:** a *shared* swap/add is screened against
  the **folded** set of every member's contraindications (Phase 1), but an `adjust`
  swap only trains the one member, so it is screened against **that member's own**
  contraindications (`validation.member_forbidden_terms`) — a movement unsafe for a
  *different* member is allowed (it never reaches them). Validation resolves the
  member (active member of *this* group; foreign/ended/unknown rejected), bounds
  `load_pct` to the override's `MIN/MAX_LOAD_PCT` (dropping a no-op 100%), and
  requires a real diff; an `adjust` on an individual plan is rejected.
  **Apply (`agent.apply._apply_adjust`)** calls `GroupMembership.set_override` — the
  same upsert the coach's click-to-adjust editor uses — so the override shows on the
  designer `adj` overlay and flows through deliver-to-all (`sync_delivered_plan`)
  with zero new apply/delivery code. `serialize_proposed_change` carries a `member`
  name for an adjust (review badge + inline chip) so the coach sees *who* diverges;
  the three serialization sites `select_related` the membership (no N+1). The cached
  system prompt is unchanged — the shared-vs-adjust guidance lives in the volatile
  `_GROUP_FRAMING` user turn (same split as Phase 1). Red→green
  (`test_group_agent_adjust.py`, +19); 1450 project pytest + 115 vitest green, ruff +
  DjHTML + `makemigrations --check` clean. **No further group-agent backlog** (both
  the shared-edit and per-athlete-adjust halves are built). **Remaining Meso
  backlog:** billing annual prices (BLOCKED on owner numbers + Stripe annual Prices);
  Anthropic Admin/Usage-API reconciliation (deferred, needs Admin key).
- 2026-06-30 — **Coach-facing billing & usage page built** (PR #356, no migration).
  The agent-usage tracking shipped its owner dashboard (Phase 4, staff-gated,
  org-wide *cost*/COGS) but a coach had no single place to see *their own* plan,
  bill, and agent usage. This adds the coach-scoped complement at `/meso/billing/`
  (`CoachBillingView`, `LoginRequiredMixin`; a non-coach is routed to their training
  home, mirroring the roster's role split): the coach's plan/tier, the bill they
  owe (base + per active seat, the seat line floored at 1 to mirror Stripe's seat
  quantity), the upgrade CTAs, and their AI-agent runs this month broken down per
  athlete/group. **Decision (the hard line):** a coach sees **what they pay**
  (revenue) and **how much they've used** (run counts), **never** the internal
  per-run cost estimate (`estimated_cost_usd`) — that COGS view stays owner-only on
  the staff dashboard. So the new `agent_usage_report.coach_run_breakdown` returns a
  `ClientRun` row carrying run counts only (no cost), reusing `_attribution` (a group
  plan → the group) and counting *all* of the coach's in-window batches so the total
  reconciles with `billing/access.agent_runs_this_month` (the free-tier meter);
  `presenters.coach_billing` composes `billing_state` + the projected bill + the
  breakdown. A "Billing" nav link rides the coach surfaces (athlete pages override
  the `navlinks` block, so it never shows to athletes). Red→green
  (`test_coach_billing.py`, +14: breakdown helper, bill math, the no-COGS-leak
  invariant, the view gate + scoping); 1342 meso pytest green, ruff + DjHTML +
  `makemigrations --check` clean; **Codex review CLEAN iter 1**; deploy success,
  prod-verified (`/meso/billing/` 302→login for anon, the route is live). **Remaining
  Meso backlog unchanged:** billing annual prices (BLOCKED on owner numbers + Stripe
  annual Prices); Anthropic Admin/Usage-API reconciliation (deferred, needs Admin key).
- 2026-06-30 — **Coach roster adherence built: the compliance meter + activity
  feed go live** (no migration). The roster carried two dead placeholders since
  Phase 1 — `presenters.roster_athlete` returned `compliance=None` and `RosterView`
  set `activity=[]`, both flagged in-code as "Phase 2/3 concepts awaiting logged
  data." Delivery + logging have existed since the athlete slice, so the data was
  finally there; this wires it. The roster **UI already existed** (the `meso-meter`
  bar + the "Recent activity" card), so the slice is **backend-only**. New
  `meso/adherence.py` (pure read-side aggregation): `link_compliance(link)` = the %
  of the **latest delivered week's** sessions the athlete marked *done*
  (`link_latest_delivered_week` spans the individual plan **and** any group-delivery
  snapshot rooted at the link — adherence to whatever was delivered most recently);
  `recent_logs(coach)` = the coach's active-link athletes' most recently completed
  sessions. **Decisions:** `None` (no delivered week) hides the meter while `0` (the
  coach delivered, the athlete hasn't logged) is a real, distinct signal that's
  kept; ordering is by `created_at` (when logged) — *not* the nullable workout
  `date`, whose NULL sort order differs SQLite↔Postgres. `presenters.roster_activity`
  shapes the feed (athlete + session + a compact "N ago" off `timesince`, since
  `humanize` isn't installed). **Profile page left untouched** — its compliance meter
  is bundled behind `has_program` with still-unfilled `block`/`week`/`macrocycle`
  placeholders, so lighting it up is a separate, larger slice. The seeded demo
  already delivers + logs Maya's week, so the one-click demo showcases the meter for
  free. Red→green (`test_adherence.py`, +25): compliance math/scoping (latest-week-
  only, own-done-only, dedup, **archived-plan exclusion**), `recent_logs` scoping/
  ordering (active links, **non-archived plans**, **athlete tied to the plan's own
  athlete**), presenter shaping, the RosterView render. 1353 meso pytest green, ruff
  + DjHTML + `makemigrations --check` clean. **Codex review loop CLEAN after 2 fix
  iters** — both real: (1 = P2) the queries didn't exclude **archived** plans, so a
  removed group member's archived snapshot (link stays active) could drive the meter/
  feed for a program the athlete can't see → excluded, matching `working_plan`/
  `athlete_home`; (2 = P2) `recent_logs` didn't tie `SessionLog.athlete` to the
  plan's relationship athlete (the write path always does, but the model has no DB
  constraint), so a stray mismatched row could leak an unrelated name + an unreachable
  profile link → added an `F()` predicate. **Remaining Meso backlog unchanged:**
  billing annual prices (BLOCKED on owner numbers + Stripe annual Prices); Anthropic
  Admin/Usage-API reconciliation (deferred, needs Admin key). **Deferred follow-up:**
  lighting up the athlete-profile program block (compliance + current block +
  macrocycle) as its own slice; per-athlete `delivered`/`needs_review`/`drafting`
  status badges (need agent/delivery state).
- 2026-06-30 — **Athlete-profile program block lit up** (PR #361, no migration).
  Closes both of #359's deferred follow-ups in one backend-only slice: the
  profile's `has_program` block (Current block · Wk N + adherence meter + the
  macrocycle rail + the Latest-session card + the left-rail Goals) and the
  per-athlete status badge. The template was already built; it was fed dead
  placeholders (`presenters.profile_athlete` returned `has_program=False`;
  `AthleteProfileView` hard-coded `macrocycle=[]` / `results_summary=None`). New
  `presenters.profile_program(link, working_plan)` (+ `_profile_status` /
  `_profile_results`) keys off `adherence.link_latest_delivered_week` — the
  athlete's *delivered* reality, the same week the roster meter measures, spanning
  the individual plan **and** any group-delivery snapshot: `block` = that week's
  mesocycle name, `week` = `Wk {index}`, `compliance` = `link_compliance`,
  `macrocycle` = `serializers._phase_states` / `serialize_mesocycle` (reused from
  `serialize_plan`) positioned at the delivered block, `status` =
  `needs_review` > `drafting` > `delivered`, `results_summary` =
  `presenters.session_results(...)["summary"]`. **Decision:** `has_program` is
  gated on a *measurable* delivered week (a week exists **and** compliance isn't
  `None`), so an undelivered / empty-week athlete falls through to the existing
  create / in-progress empty state rather than a half-lit block; the goal still
  surfaces pre-delivery from the working-or-delivered plan. The view merges the
  program overlay onto the `profile_athlete` identity dict (the same pattern
  `deliver_screen` uses). **The Codex review loop's three fix iters were all one
  class — a now-data-rich card must point every link at an athlete/session-specific
  *and authorized* target, never a bare redirect:** (1, P2) the "Review agent
  changes" CTA linked to bare `meso:review`, which redirects to the coach's
  globally-latest pending batch (possibly a different athlete) → thread *this*
  athlete's newest pending-batch id and link to `review_batch`; (1b) the
  Latest-session card rendered unconditionally inside `has_program`, so a
  delivered-but-unlogged program showed a blank card with a dangling `%` → gate the
  card on `results_summary` (and the warning chip on a non-empty `flag`); (2, P2)
  the card linked to bare `meso:results` (the coach's globally-latest logged
  session) → thread the `session_id` and link to `results_session`; (3, P2) the card
  could target a materialized group-delivery snapshot session, but `ResultsView`
  authorizes through `Plan.objects.for_coach` (individual-only — excludes
  `source_group`), so the link would 404 → scope `_profile_results` to non-snapshot
  (individual) plans (the block/adherence still light up off the snapshot; only the
  unopenable card is hidden). Red→green (`test_profile_program.py`, +27): the
  empty/in-progress/delivered states, block/week labels, macrocycle states, status
  precedence + batch-id threading + leak-scoping, results scoring + own-athlete +
  draft + snapshot exclusion, the group-snapshot block, and the rendered page. 1380
  meso pytest green, ruff + DjHTML + `makemigrations --check` clean. **Codex review
  loop CLEAN after 3 fix iters.** Deploy success; prod healthy (`/meso/` 200,
  `/meso/me/` 302). **Remaining Meso backlog:** billing annual prices (BLOCKED on
  owner numbers + Stripe annual Prices); Anthropic Admin/Usage-API reconciliation
  (deferred, needs Admin key). Autonomous slices still open: roster
  relationship-history view; push re-deliver debounce.
- 2026-06-30 — **Relationship-history view built & deployed** (PR #364, no
  migration). Closes the "roster relationship-history view" autonomous slice. An
  ended or declined `CoachAthlete` link vanished from the active roster
  (`RosterView` queries only `.active()`), though the row + its `ended_at` +
  the archived plans all persist — a coach had no way to see past athletes or
  re-engage them. New surface: `CoachAthleteQuerySet.closed()` (ended + declined)
  + `CoachAthlete.is_closed`/`closed_at` (ended_at for ended, responded_at for
  declined); `presenters.relationship_history(coach)` splits one query into
  **past** (ended/declined, newest-closed first, re-invitable) and
  **reconnecting** (coach-side `pending_coach_invite` re-invites awaiting the
  athlete — surfaced nowhere else), demo links excluded;
  `RelationshipHistoryView` (`/meso/history/`, login + coach-only, non-coach →
  training home, mirroring `RosterView`); `relationship_reinvite` (POST) reopens
  a closed link to a fresh `pending_coach_invite` via the existing
  `CoachAthlete.invite` primitive (row-locked under explicit `transaction.atomic`),
  **seat-gated** (D4 — accepting would create a billable seat), closed-only
  no-op, coach-scoped 404. The re-invited athlete (already a registered user)
  discovers the invite on their training home (the canonical peer-invite surface
  — `athlete_pending`). A discreet "Past athletes" link on the roster +
  `relationship_history.html`; a seeded demo past athlete so the surface shows on
  a fresh DB (billing-neutral — an ended link isn't a billable seat). **No
  migration** (QuerySet method + properties only). +24 tests
  (`test_relationship_history.py` + seed coverage); 1404 meso pytest green, ruff +
  DjHTML + `makemigrations --check` clean. **Codex review loop CLEAN after 1 fix
  iter** — the P2: a re-invite reopens the row in place, so `created_at` is the
  *original* relationship date; the reconnecting surface now shows **state**
  ("Awaiting their reply"), not a misleading "Re-invited {date}", and sorts by
  `created_at` only for a stable order (a true reopen timestamp would need a
  migration — not worth it for a P2, per the project's defer-new-tables taste).
  **The "push re-deliver debounce" slice was already YAGNI-skipped** (2026-06-29
  review: mitigated by the push `tag` collapse), so the autonomous Meso backlog is
  now: billing annual prices (BLOCKED on owner numbers + Stripe annual Prices) and
  the Anthropic Admin/Usage-API reconciliation (deferred, needs an Admin key).
- 2026-06-30 — **Billing pivot: base+per-seat → a single FLAT $19/mo Pro plan
  (D14, billing Phase 7, migration `0028`).** The "unblock annual prices" task
  surfaced a deeper decision: annualizing a monthly-fluctuating seat count is
  awkward (Stripe can't mix monthly + annual lines in one subscription — annual
  base + monthly seats would need *two* subscriptions), and stepping back, the
  **only cost that scales with usage is the AI agent** (Claude, ~$0.05–0.10/run) —
  athletes/groups/storage/delivery are ~$0 marginal — so per-seat pricing never
  tracked cost, it just dragged in complexity (seat sync, the daily
  `reconcile_seats` sweep, proration, two-line-item Checkout). **Owner chose a flat
  plan.** New shape: **$19/mo flat, unlimited athletes**, one Stripe Price
  (`MESO_PRO_PRICE_ID`, qty 1), supersedes D13. The **AI agent is now metered at
  every tier** to bound worst-case COGS per coach (cap × ~$0.10): free
  `FREE_AGENT_ALLOWANCE` (5)/mo, trial/active `PAID_AGENT_ALLOWANCE` (150)/mo,
  comped uncapped — `access.agent_allowance`/`agent_runs_remaining` generalize the
  old free-only meter; the endpoint 402 + designer/roster/coach-billing meters are
  tier-aware (free → upgrade CTA; paid → monthly-reset note). The seat-sync/annual
  machinery was **removed** (`billing/seats.py`, `sync_seat_quantity`,
  `meso_reconcile_seats` + its schedule, dropped in `0028`; the two-line-item
  webhook classifier) — dead + a footgun that would mis-resize the flat plan. The
  free-tier seat **cap** and the D6 downgrade suspension **stay** (only the per-seat
  *charge* is gone). Revenue math → flat `PRO_PRICE_USD`; `PRICE_SUMMARY` → "$19/mo
  — unlimited athletes". The already-built agent-usage tracking (`/meso/usage/`)
  measures real per-coach cost, so per-seat/annual can be reconsidered later from
  data. Ships **dormant** until the owner creates the one Price + registers the
  webhook. Red→green (`test_billing_flat.py` + reworked `test_billing_stripe.py`;
  `test_billing_phase6.py` removed); 1582 project pytest green, ruff + DjHTML +
  `makemigrations --check` clean. Full detail in [`billing-plan.md`](./billing-plan.md)
  (D14 / Phase 7). **No autonomous billing backlog remains** (annual deferred).
- 2026-07-16 — **Spreadsheet parity Phase 2a: the text-first cell shipped
  (migrations `0038`/`0039`).** A `Prescription` cell is now one freeform
  `text` string per `(exercise_slot, week, line)` — line 0 = the prescription
  (`4 x 6, RPE 9, 225`), lines 1+ = freeform sub-rows (the templates' RPE row,
  cues, logged deviations, substitutions) — with structure derived on demand by
  the new `parsing.parse_prescription` (never persisted as truth). Retired:
  `sets`/`reps`/`load`/`load_type`/`rpe` + per-week `rest`/`note` +
  `swap_exercise`/`swap_name` (a swap/skip/note is typed text per plan §2.6;
  the structured `skipped` em-dash flag stays per §2.1), the `LoadType` enum,
  the `prescription_swap` endpoint, and the designer's %1RM editor/`load_type`
  toggle (D5 defers %1RM; athlete-side 1RM endpoints stay). Added:
  `ExerciseSlot.tempo/rest/note` (per-exercise columns, D2), `cell_line_write`
  (sub-line upsert addressed by slot/week/line) + `exercise_slot_patch`
  endpoints, and blank starter cells (spreadsheet semantics — a cleared
  sub-line is blank text, never a deleted row). The migration composes every
  existing cell into Lance-notation text, hoists rest to the row (modal value;
  divergent weeks keep theirs inline), converts `WeekDelivery` payloads, and
  **wipes the `PlanAction` undo/redo stacks** (old snapshots capture retired
  columns and can't replay); `restore_plan_snapshot` now UPSERTS cells by pk so
  redo can revive deleted sub-lines. Group overrides (until 2c removes them,
  D1) resolve as text: volume recomposes line 0 via the parser; swap/note/
  load-% become extra sub-lines on the member's materialized copy. Agent
  progress/volume applies parse-and-recompose the cell text (unparseable
  notation = safe skip); the %1RM progression guard keys off the target cell's
  parsed `%` load. Full design in
  [`spreadsheet-parity-plan.md`](./spreadsheet-parity-plan.md) §2/§6.
- 2026-07-16 — **2c built: the group subsystem is REMOVED, replaced by
  batch-deliver (D1, parity plan §3.1; migration `meso.0040`).** Deleted
  wholesale: `MesoGroup`/`GroupMembership`/`PrescriptionOverride`,
  `Plan.group`/`source_group` + the XOR/singleton constraints (a
  `relationship` is the only plan root now; `editable_by` == `for_coach`),
  `deliver_block`/`sync_delivered_plan` (the fan-out materializer), the
  override endpoint + the designer's override editor/`adj` overlay, the group
  designer mode (the island is single-mode; `group`/`adj`/`adjusts` left the
  grid payload), the group agent surface (`Kind.ADJUST`,
  `ProposedChange.membership`, `Trigger.GROUP`, `_group_context`, the member
  framing + adjust tool schema), history's override snapshots, usage-report
  group attribution (`is_group` dropped; every client row is an athlete), the
  roster Groups card, the demo "group" segment, the "groups" tour step (self
  tour = 7 steps), and the seeded demo group. ~211 group tests deleted; 18
  shared suites de-grouped. The migration's data step deletes the shared
  group plans (relationship NULL — demo-only in practice); a member's
  materialized group-delivery plan keeps its relationship root and survives
  as an ordinary individual plan — exactly the new model. Historical
  `trigger="group"`/`kind="adjust"` ledger rows are left untouched (choices
  aren't DB-enforced; rewriting would falsify usage history).
  **The replacement:** `Plan.duplicate_for(relationship)` deep-copies the
  live tree (whole line stacks incl. sub-lines/`skipped`, tempo/rest/note,
  tags, catalog FKs; `is_current` mirrored; `delivered_at` reset; soft-deleted
  rows stay behind) and `plan_batch_deliver` (form POST from the deliver
  screen's new "Also deliver a copy to…" card) fans out one independent,
  live-editable ACTIVE copy per picked client — stamped + `WeekDelivery`
  snapshotted + block-nudged exactly like an individual deliver, own-athlete/
  foreign/seat-suspended picks dropped, the whole batch in one explicit
  `transaction.atomic()` (ATOMIC_REQUESTS is inert). The optional saved
  client list ("one-click class deliver") is deferred until real use demands
  it. Follow-up debt for 2e: dead group CSS in the designer stylesheets.
- 2026-07-16 — **Built (spreadsheet parity 2d): deliver → live + notify — the
  delivery visibility gate is gone (D6, parity plan §3.3).** This supersedes
  the 2026-06-28 "delivery contract" (delivery gates a week's *visibility*):
  the athlete now sees **every live week** of every non-archived plan through
  an active coach link the moment the coach types it — a shared spreadsheet.
  Gate removals: `serializers.latest_delivered_week` deleted; the athlete
  home/chips/block-grid/focus-override and `_athlete_session_or_404` (session
  view + logger + manual-1RM) dropped their `delivered_at` filters; the home
  anchors on the `is_current` pointer (first flagged in plan order, else the
  earliest live week; `awaiting` = a plan with no live weeks at all).
  Adherence re-based: `link_latest_delivered_week` →
  `adherence.link_current_week` (newest non-archived plan by `modified`, then
  its flagged current week) — the roster meter and profile-program block now
  measure the athlete's *current* week. **Deliver stays as the one-time
  nudge:** stamps `delivered_at` (a notify marker only), writes `WeekDelivery`
  snapshots (history/retention; they feed the deliver screen's what-changed
  diff, now explicitly optional, and later the PR engine), and sends the one
  block-level email + push. The dead per-week notify chain was removed
  (`_notify_athlete_delivered`, `emails.send_week_delivered_email`,
  `push.notify_week_delivered`, the `week_delivered.*` templates — the block
  variants are the only notifiers since P3). Copy reframed on the deliver
  screen ("your edits are already live … delivering sends a heads-up and
  records a snapshot"; stale "Schedule & notifications" placeholder card
  dropped), the athlete home, the designer's AthletePreview coachmark, and
  the "Make current" tooltip. Tour/demo delivery checks are untouched (they
  test "did the coach send the nudge"). No migration — `delivered_at` and
  `WeekDelivery` keep their data and meaning as history.
- 2026-07-16 — **Built (spreadsheet parity 2e): UI cleanup — the dead chrome
  from 2a–2d is stripped (parity plan §6, Phase 2 COMPLETE).** CSS-only by
  construction: the earlier slices already deleted the components and
  endpoints; a mechanical sweep (every class defined in the designer
  stylesheets + `meso.css` checked against all TSX/HTML/JS/PY usage,
  including template-literal `--${state}` construction) found ~75 orphaned
  classes and zero orphaned markup. Deleted: **`designer-modal.css`
  wholesale** (the 2c per-athlete override-editor modal — backdrop/field
  grid/member picker/save-clear-cancel buttons — was its only tenant; the
  `@import` dropped from `designer.css`); the rest of the **group chrome**
  (topbar `meso-group-avatar*` stack, rail group avatar/glyph +
  `--tight` list, `meso-participant-*` rows, the chat change-card's
  `meso-change-member` chip, the grid's `meso-adjust-*` badges — the
  "dead group CSS" debt logged under 2c); the **structured-cell chrome**
  (2a: the `meso-onerm-*` editor family, `meso-load-toggle`,
  `meso-num-input`, `meso-note` + its `meso.css` focus twin, the
  `meso-table-cell-setsreps/load` flex pair, the swap badge/editor/input
  family); the **retired one-week-designer chrome** (A5: `meso-week-view*`,
  `meso-canvas-autosaved*`); and **prototype leftovers** (`meso-chip-soon`,
  `meso-flag-badge/dot`, `meso-grid--2`, `meso-inline-block` —
  `meso-seg-btn--v/--p` looked dead to the first sweep but are live in
  `DesignerRoot.tsx`/`BlockView.tsx` template literals and were restored;
  the sweep's meso.css corpus was missing `.tsx`, caught by the Codex
  review loop). Stale comments that named the dead classes
  (the six-cell a11y note, the RowOneRmEditor/2nd-line notes, the
  skip/swap cluster label, the day-card drag mirror) were rewritten in
  place. Verification: the sweep re-run reports 0 dead classes; dist
  rebuilt (designer.css 22.3 kB); 530 vitest + 2138 pytest green.
- 2026-07-17 — **Built (spreadsheet parity Phase 3): import + validate —
  template plans + the workbook importer (parity plan §5, §3.4).** This
  resolves the deferred Q2 "template library" as designed: a template is a
  **`Plan` with `is_template=True`, no relationship, and an `owner`** (the
  coach whose library it belongs to; new FK `related_name="template_plans"`,
  migration `0041`, plus a check constraint `template_plan_has_no_relationship`
  — a template can never carry a relationship). The relationship-less crash
  paths are fixed (closing the 2c-era Codex finding on the nullable FK):
  `Plan.__str__` falls back to the bare title ("… (template)"), `Plan.coach`
  returns the `owner` when there's no relationship (may be None — callers
  beware), and `is_editable_by` / `PlanQuerySet.editable_by` grant the
  template's owner — so the owner opens and edits a template **in the same
  designer grid** (no second editor): `serialize_athlete_identity` shows the
  template's own title + "Template" in the identity chip, the deliver screen
  bounces a template back to the designer, and `plan_deliver` /
  `agent_propose` / `coach_set_one_rm` refuse cleanly (no athlete to nudge /
  ground on / own a 1RM). `plan_batch_deliver` **works from a template** at
  the endpoint level (each pick gets an independent `duplicate_for` copy) but
  has no screen — noted as the gap. **The importer:** `meso/sheet_import.py`
  (pure **openpyxl** — a new runtime dependency) parses a Drive-exported
  template workbook into the exact `build_block` spec (`ParsedBlock` =
  block_spec + tab/week-count/skipped-row report): picks the visible program
  tab (header row carrying `Exercise` + `Week N` labels — 102's hidden legacy
  tab and 101's Athlete/Warm Up/FAQ/Periodization tabs are passed over),
  resolves columns from header **labels** per Day section (letters drift),
  reads exercise blocks by the name column's **merge extents** (name/tempo/
  coach-comment→`note`/rest are merged down the block; the blank set-detail
  log rows inside are skipped; float tempos coerce `201.0`→`"201"`), folds
  any non-empty week-column row inside a block (the newer templates' RPE row)
  into per-week **sub-lines** (line 1+), imports full-width in-grid
  separators (601's `Rest 5 minutes`) as cell-less freeform rows and packed
  EDT/circuit cells verbatim as one row, and skips + reports banners/`Date:`
  rows/`END OF WEEK` footers — unknown structure is reported, never raised.
  `manage.py meso_import_template <xlsx>... --owner <email> [--title]`
  wraps it: ONE template plan, one `Mesocycle` per file in argument order
  (101→102→103 = one 3-block plan), atomic, with a per-file
  days/exercises/weeks/cells + skipped-rows summary. **A same-title re-run
  fully REBUILDS the tree** (Codex review finding): the workbook is the
  source of truth for a template, so re-importing must also *remove* what
  the source no longer has (a shrunk family, a deleted exercise row, a
  shortened sub-line stack, a dropped week) and may legitimately clobber
  designer edits — an upsert-only re-run left that stale programming in
  place. The rebuild deletes the plan's mesocycles (the whole tree
  cascades) and wipes the plan's `PlanAction` undo/redo stacks — their
  plan-wide snapshots reference the deleted pks and would resurrect ghost
  rows on undo (the 0038/0039 wipe precedent). Chosen over a surgical
  prune: simpler, semantics-clean, and pk stability buys nothing for a
  library object with no athlete logs or deliveries hanging off it.
  Validated end-to-end over the five raw fixtures (now all committed:
  `docs/meso/fixtures/templates/{101,102,103,402,601}.xlsx`): 7 days each,
  22/22/22/19/13 exercises, 96/100/100/72/40 non-empty cells. Deliberately
  NOT built: a template-library UI, a "new from template" button, and any
  template-awareness on athlete surfaces (they're relationship-rooted;
  a guard test proves templates never appear there). 2183 pytest green.
- 2026-07-17 — **Built (spreadsheet parity 4a): athlete tracking via freeform
  sub-lines — undo-isolated from the coach (parity plan §2.4/§2.6, phase 4).**
  The athlete's delivered session gains an *editable* sub-line stack beneath
  each exercise: `POST /meso/api/me/session/<id>/cell/` (`athlete_cell_write`)
  upserts the same `(exercise_slot × week × line)` cell the coach's
  `cell_line_write` addresses, saved on blur. **Decision — the undo
  interaction:** athlete sub-line writes record **no** `PlanAction` (the
  athlete moving/annotating their own training is not a coach designer edit and
  must not enter the coach's undo stack — same principle as
  `advance_current_week` and `athlete_log_session`), and each write stamps the
  new flag **`Prescription.athlete_authored=True`** to keep the cell out of the
  coach's undo/redo snapshot machinery entirely. Without the flag a coach undo
  WOULD clobber athlete writes — `serialize_plan_snapshot` captures every cell
  by pk and `restore_plan_snapshot` upserts every snapshotted cell's text and
  hard-deletes any live cell absent from the snapshot, so an athlete note made
  after the coach's last snapshot would be reverted or deleted (silent data
  loss). Three `history.py` guards close it: snapshot capture excludes
  `athlete_authored=True`; restore's upsert skips any pk whose **CURRENT DB
  row** is athlete-authored (not the snapshot's flag — an older coach snapshot
  still holding a coach version of that pk must not overwrite a later athlete
  edit); the stray-cell hard-delete excludes athlete-authored. A coach edit to
  the same cell (`cell_line_write`) **reclaims** it — flips `athlete_authored`
  back to `False`, folding it into coach history again. The coach still sees
  athlete lines live (the designer reads cells via `serialize_plan`, not
  snapshots). The endpoint mirrors `athlete_log_session`: athlete-scoped
  (foreign/archived/unknown → flat 404), **no billing gate** (the coach's
  over-limit freeze never touches the athlete's own tracking), body validated
  before any write (line 0 and line > `MAX_CELL_LINE` are 400s; blank text
  clears in place), idempotent upsert, and it **advances `is_current`
  forward-only**. Presenter threads per-exercise `sub_lines` + `cell_url` and
  folds only line 0 into the read-only `target` so the editable stack isn't
  double-displayed. Migration `0042` (additive boolean, no backfill —
  existing cells are coach-authored). Full meso suite green (1976).
- 2026-07-17 — **Built (spreadsheet parity 4b): personal records — derivation
  (parity plan §6, phase 4).** First slice of the parse-layer /
  prescribed-vs-performed thread. **Decision — the performed source (the "live"
  D4 call):** personal records ride on the **structured `LoggedSet` performed
  record, NOT parsed free text** (Lance, 2026-07-17: keep a structured
  representation of a logged/performed set — PRs are easier to manage off it).
  The two athlete write-paths that coexisted since 4a — structured `LoggedSet`
  and freeform sub-lines — are hereby role-split: `LoggedSet` is the performed
  store PRs read; sub-lines stay as notes/annotations for now. **Decision — the
  parse pipeline (when/into-what, previously never pinned):** free text parses
  **at commit time into a `LoggedSet`** (not lazily on read as `parse_prescription`
  does today), collapsing the two entry paths into one structured store — but
  that pipeline is a deliberate *later* slice; 4b ships PRs on the already-populated
  `LoggedSet` at zero parsing risk. The estimated-1RM number already exists
  (`AthleteOneRm` + `one_rm.derive_one_rm_values`); 4b adds what makes a *record*:
  **provenance** (which logged set/date achieved the best) and **new-PR
  detection**. New pure module `personal_records.py` (derive-on-read, nothing
  persisted — a `PersonalRecord` table is a later slice): `personal_records(
  athlete, *, unit)` → best e1RM per lift with provenance;
  `new_records_in(session_log)` → pure detection (no writes, no `PlanAction`) vs
  the prior best **excluding the session under test** (first-ever log = a PR, not
  a self-tie). Reuses `one_rm.epley_one_rm` verbatim + the B4 identity
  (`one_rm.key_str` → `serializers._exercise_key`); DONE-only, unit-scoped exactly
  as `derive_one_rm_values`. **Seam** (`_best_per_lift` over normalized
  `_PerformedSet` tuples; `_performed_sets` the sole `LoggedSet`→tuple bridge) so
  the future parse-at-commit feed drives the same computation with no rework.
  Backend-only, no UI (the PR surface is the next slice), no migration (stays
  `0042`). 14 pinned tests (Epley tie, provenance, identity keying, unit scoping,
  DONE-only, non-numeric skipped, new-PR true/false/tie/exclude-self). Full meso
  suite green (1991). Athlete session viewer judged good as-is; the **program
  designer needs a separate UX-cleanup pass** (overloaded layout + an unnecessary
  current-week selector) — handed to its own session.
- 2026-07-17 — **Built (spreadsheet parity, designer simplification): grid-first
  UX cleanup (PR #477).** The separate designer UX-cleanup pass flagged at the end
  of the 4b entry, resolved as an approved Full Tiers 1–4 + Passes E/F. The designer
  had grown three permanent columns (left rail + agent + grid), always-on per-cell
  chrome, and day tables that didn't align across days; the grid is the product, so
  give it the screen and let it read like a sheet at rest. **Layout:** left rail +
  agent merged into ONE ~344px sidebar (slim athlete-metadata header above the
  always-visible agent — the agent ships soon, so it stays visible by default); grid
  canvas 818px → 1096px at 1440px, collapsible to full width; the
  Table/Periodization/Athlete switcher folded into the top bar (reclaims the 49px
  canvas-header band); duplicate identity chip + 3× goal renders removed; Undo/Redo
  moved to the top bar as ↺/↻ icons (kept `data-grid-restore` so focus returns to
  the grid). **Grid feel:** resting cells read like a spreadsheet — Skip / Fill→ /
  "+ line" reveal only on cell `:focus-within` (no hover reflow), per-row × on row
  hover; the always-on "The block table" info alert dropped. **Week management:**
  week Make-current / Remove moved OUT of the day-table headers into a
  mesocycle-level `WeekManagerStrip` above the tables (rendered ONCE, not repeated
  ~12× per day header); `WeekColumnHeader` is label-only. **Table structure:**
  `table-layout: fixed` + a shared `<colgroup>` from one width source so every day's
  table aligns column-for-column; fixed a real separator-misalignment bug — a
  `display:flex` `<td>` detaches from table row-height sync, leaving a tall sub-line
  row's sticky border misaligned. **Removed** the cross-day "Move to…" select +
  `moveExerciseToDay` verb (drag is within-day only; delete + re-add is the
  workaround). **GUARDRAIL preserved** — `is_current` semantics, deliver→live,
  billing, the athlete session viewer, and the agent's propose→review→apply contract
  all untouched (every change is UI-layer); the clutter was the per-day×per-week
  repetition, so de-duped, not deleted. 519 frontend tests green; `tsc` clean; Codex
  CLEAN — it caught one real regression (hiding the ghost `+line` with `display:none`
  broke keyboard ArrowUp into it; jsdom can't see CSS so tests passed falsely) fixed
  with a zero-height *focusable* collapse. **Reusable lesson: never `display:none` a
  keyboard-nav grid stop — use a focusable collapse.** No migration.
- 2026-07-17 — **Built (spreadsheet parity 4c): PR event surface — athlete toast +
  coach results marker (parity plan §6, phase 4).** PR #1 of the PR-surface slice.
  The 4b engine (`personal_records.py`) shipped derive-on-read best-e1RM +
  `new_records_in` detection with no UI; 4c surfaces the detector at the two moments
  a new best matters. **Athlete:** `athlete_log_session` returns `new_records`
  alongside `log` (pure detection off the just-committed rows, DONE-only so a "Save
  progress" draft returns `[]`); `meso_athlete.js` populates it in `save()` and the
  offline `flushQueue()` path (a PR beaten offline still lands on sync);
  `athlete_session.html` shows a dismissible 🎉 celebration card. **Coach:**
  `session_results` adds `summary["new_records"]` + a per-row `pr` flag matched by
  the same B4 lift identity (`key_str`) the engine keys on; `results.html` renders a
  "New PR(s) this session" callout + an inline `PR` badge. **Shared:**
  `serialize_new_record` formats the raw Epley floats **server-side** (2-dp,
  trailing-zero trimmed, matching the `AthleteOneRm` display) so the client renders
  verbatim and can never re-round to a different value than the pinned server one.
  Source of truth = the structured `LoggedSet` (D4), not parsed free text; reuses
  the pinned `epley_one_rm` + hybrid B4 identity verbatim. Derive-on-read, no
  `PersonalRecord` table (a deliberate later slice), no new endpoints/URLs, no
  migration (stays `0042`). 10 tests (`test_pr_surface.py`); full meso suite 1998;
  Codex CLEAN.
- 2026-07-17 — **Built (spreadsheet parity 4d): personal-records panel — athlete
  home + coach profile (parity plan §6, phase 4).** PR #2 of the PR-surface slice —
  the persistent "records book" that completes it (event + standing bests). A
  **Personal records panel** on the athlete's training home and the coach's
  athlete-profile, both fed by 4b's derive-on-read `personal_records()` (best Epley
  e1RM per lift + the winning set's provenance). `presenters.py`:
  `_personal_record_rows(athlete, unit)` (shared, alphabetical, e1RM formatted
  server-side via `_fmt_num`), `athlete_personal_records(user)` +
  `coach_personal_records(link)`, each scoping unit via `_records_unit_plan`.
  `views.py`: `AthleteHomeView` + `AthleteProfileView` set
  `ctx["personal_records"]`. `_pr_list.html`: one shared partial (lift · est. 1RM ·
  provenance), self-hiding when empty, included below the live programs (athlete)
  and in the left rail (coach). **Decision — unit is a per-PLAN property** (there is
  no athlete-level unit preference), so each host shows one denomination (its
  most-recently-active plan's unit) rather than pooling kg and lb; the coach reaches
  the panel only through an active link. Derive-on-read, nothing persisted, no new
  endpoints/URLs, no migration (stays `0042`). 11 tests (`test_pr_records_panel.py`);
  full meso suite 2009; Codex CLEAN. **The PR-surface slice is complete — the plan's
  runway is now parse-at-commit → agent.**
- 2026-09-18 — **Built (#506, first slice): local, opt-in browser E2E suite.**
  `e2e/` runs only through `just e2e` (see "E2E browser tests" above). Three
  journeys, each at desktop and phone: the real allauth login, an athlete
  typing `100 x 5` into a sub-line and logging the session, and the coach
  reading that set on the results page. Each journey was checked by breaking
  the behavior it covers (no blur save, no badge update, no parsed set, the
  logged cell blanked) and watching it fail. `data-testid`s added only where a
  journey needed one. No app behavior changed, no migration.
- 2026-09-19 — **Built (#508, first slice): the athlete pages on a phone.**
  Training home and session logger only; coach pages and the designer
  fallback are the next slice. Under 640px the block table becomes stacked
  day cards showing the week the chips select, each prescription line on its
  own line (`_cell_lines` in `presenters.py`; `_text_label` still returns one
  string for the logger target and coach results). The wide table keeps a
  sticky header row. It pins inside the table's own scroller, which is capped
  at 70vh, because an `overflow-x:auto` box is a scroll container on both axes
  and a sticky cell can't pin to the page from inside one. **Decision: every
  input on an athlete page is 16px at every width, not just under a
  breakpoint.** iOS zooms on focus at any width, and a phone held sideways is
  wider than 760px. Zoom stays enabled. Tap targets grow to 44px under 760px
  or on any `pointer: coarse` device, scoped to `.meso-athlete` except the
  topnav's brand and links (shared chrome, already 55px tall, so nothing
  moves). The logger's set row is a grid, so it fits at 320px. Neither logging
  path changed behavior. E2E gained a 360×780 viewport and a layout journey
  per page, each checked by breaking the rule it covers. No migration.
- 2026-09-19 — **Built (#508, second slice): the coach pages on a phone, and
  a phone fallback for the designer.** Roster, results, the athlete profile,
  deliver, review and the template library share one breakpoint at 760px
  (nav.css's burger breakpoint), scoped by `.meso-coach` in its own section at
  the end of `meso.css`. Grids go to one column, inline flex rows marked
  `.meso-phone-wrap` wrap, and each results row stacks under its exercise name
  with a label per cell (CSS `::before` from `data-label`, so the cells' text
  is unchanged). The topnav is shared chrome: on a phone its page buttons
  (`{% block topnav_actions %}`, now inside a `display: contents` wrapper)
  take a second row and the links wrap inside their own box, so no page
  scrolls sideways at 360px. A long unbroken name (an athlete with no name
  shows their email's local part; an imported plan title can have no spaces)
  wraps too: `overflow-wrap: anywhere` on the page, because `break-word`
  doesn't lower a flex item's minimum width. Desktop screenshots at 1280×720
  are pixel-identical to main.
  **Decision: the designer is not made editable on a phone.** Under 900px
  `designer.html` hides the island's mount and shows a server-rendered message
  with links to deliver the block (the island's own `?week=` target), the
  athlete's profile, and the roster; a template gets Templates and the roster
  instead. Plain HTML and a media query rather than a React branch: it's there
  before the bundle loads, and the island still mounts underneath, so a window
  that grows past 900px switches straight to the editor. The mount now scrolls
  (`overflow: auto`; the 1240px floor stays on the island's own root) instead
  of clipping, so nothing is unreachable between 900 and 1240px. There is no
  separate "athlete preview" page (the preview is a panel inside the
  designer), so the fallback links the athlete's profile.
  `e2e/test_meso_coach_mobile.py` checks each page at all three sizes, the
  fallback's links for a client plan and a template, and long names. Each
  check was shown to fail with the rule it covers broken, and the page
  checks also fail against main's files. No migration.
- 2026-09-19 — **Built (#506, second slice): the coach's core paths in the
  E2E suite.** Three journeys, each at all three sizes. The coach edits a
  sub-line in the designer, reloads, delivers the block, and the athlete
  opens the heads-up email's link and sees the edit on training home and the
  session page. The designer half always runs at desktop in its own browser
  context (`new_page(desktop=True)`), because phones get the fallback. A
  coach invites a brand-new person, who signs up through the real allauth
  form and accepts. A free coach at the one-athlete cap tries to invite and
  gets the seat-limit message and no invite. Delivery hasn't been a
  visibility gate since 2d, and nothing on screen reads `Week.delivered_at`
  with one plan, so the deliver journey checks that stamp on the model. The
  invite journey found two bugs: Accept on the claim page 403s, because its
  `no-referrer` meta makes the browser send `Origin: null` (#522), and the
  login page's "Sign up" link drops `?next` (#523). The full invite journey
  is a strict xfail on both. A passing test covers the invite, the email,
  the signup and reopening the link, and the full journey was run green with
  both fixes applied locally. #524 asks whether a coach's sub-line belongs
  under the athlete's "what you did". Each journey was checked by breaking
  what it covers. No app behavior changed, no migration.
- 2026-09-19 — **Fixed (#522, #523): a new athlete can get from an emailed
  invite onto the coach's roster.** The claim page's referrer meta is now
  `same-origin` instead of `no-referrer`. Under `no-referrer` the browser sends
  `Origin: null` on the page's own POST and CSRF rejected Accept and Decline
  with a 403. `same-origin` still keeps the token in the URL away from font and
  CDN hosts. It stays a meta, not a response header, because Caddy replaces the
  `Referrer-Policy` header in production and the meta overrides it. The login
  page's "Sign up" link is allauth's `{{ signup_url }}`, so `?next` survives
  signup and the invitee lands back on the claim page. The challenge page's
  logged-out login box had the same bare signup link and now passes `next`
  too. The invite journey's strict xfail is gone, along with the half-journey
  test that stood in for it. New fast tests: the claim page's policy, a claim
  POST under enforced CSRF with the Origin a browser sends under that policy,
  and both signup links. No migration.
- 2026-09-19 — **Built (#506, third slice): the agent, offline logging and
  the settle sweep in the E2E suite.** This finishes the issue's "Later
  journeys" list. A subscribed coach sends the agent a request from the
  designer, gets the fake agent's three changes and a review link, approves
  the swap and the load bump, rejects the set trim, and applies. Back in the
  designer, and after a reload, the grid shows the new exercise name and load,
  and the rejected trim didn't land. That journey runs at desktop only. At
  every viewport, an athlete with the network cut fills a Set row and presses
  "Log session": "Saved offline" shows and the queue holds the save. Back
  online, the queue drains, and after a reload the session is Logged with the
  set, on the page and in the database. A PENDING log with a typed set,
  settled by `settle.settle_log` as the sweep would, reads "Logged" on the
  athlete's session page, and the coach's results show the set. A settled log
  looks exactly like a tapped one on both pages, since neither reads anything
  but the status. The offline journey found #527: a line typed offline under
  "what you did" isn't queued and nothing retries it on reconnect, so after
  "Log session" the athlete gets "Saved ✓" and a Logged session with no set.
  That test is a strict xfail on #527 and asserts only the outcome, so a fix
  makes it XPASS. One testid, `review-change` on each review card. Each
  journey was checked by breaking what it covers. No app behavior changed, no
  migration.
- 2026-09-19 — **First-party usage events, first slice (#509).** New `analytics.Event`
  table and `analytics.track.track()` helper (see "First-party usage events" above),
  called at 14 server-side moments: plan_created, template_imported,
  agent_proposal_run, batch_applied, block_delivered, session_opened, set_logged,
  session_completed, invite_sent, invite_accepted, coach_request_sent,
  subscription_started, subscription_cancelled, push_subscribed. Sandbox and staff
  actors are dropped at the helper, which `TourEvent` never did. Unknown names raise
  under `ANALYTICS_STRICT_EVENT_NAMES` (tests, DEBUG) and are logged and dropped in
  production. Migration `analytics.0001_initial`. No dashboard, beacon or
  notification ledger yet; GA untouched.
- 2026-09-19 — **Product analytics dashboard and Event retention (#509, second slice).**
  `/meso/analytics/` for staff: active coaches and athletes (WAU, MAU, window), the
  invite → accept → first block → first set funnel with median times, feature adoption,
  and open/click for the Meso emails (see "Product analytics dashboard" above). It reads
  the source tables where they already record the fact and `Event` only for the rest.
  The funnel also reads coach-accepted athlete requests from `CoachAthlete`, which
  answers #542's question without a new event. Raw events older than 13 months are
  deleted daily in 1,000-row batches (`analytics.0002` registers the schedule). No
  rollup table.
- 2026-09-19 — **Fixed (#541): restoring a reclaimed line after "Log session"
  logs the set once.** Reproduced before fixing, in a Django test and in a real
  browser. The athlete types `225 x 5` on sub-line 1 and the coach rewrites that
  line. The athlete's page now shows the coach's text on the line and the set as
  a filled, checked Set row, both at once. "Log session" posts that row, which
  replaces the parsed row with a source-less structured copy. When the athlete
  types `225 x 5` back on the line, the restore lookup in `_upsert_parsed_set`
  searched only rows whose `source_line` is that line, so it created a second
  row. One set then counted twice in results, 1RM, the agent's grounding and
  `set_logged`. No stale tab is needed, unlike the "repost-then-restore" case the
  5a review deferred.
  **Rule (the 5a plan doesn't cover this case):** the structured copy survives,
  and the restore re-links it to the line. That is the state a restore reaches
  when no "Log session" happened in between: one row with `source_line` set, shown
  by the line and not by the logger, with no new `set_logged` and no PR toast.
  The link is `LoggedSet.reclaimed_line` (migration `0048`). It's only a hint
  for this lookup, so it has no database constraint, the same call as the
  `analytics.Event` FK. "Log session" records it whenever it replaces a visible
  parsed row, however the row got that way: a coach reclaim, a coach fill over
  the line, or a line the athlete edited while its row was skipped. The restore
  looks only at the copy linked to this line. A same-valued structured row with
  no link, such as a set entered separately in the logger, is never merged. On
  the linked line, typing the copy's values back counts as a restore by design,
  the same rule the older lookup applies when no "Log session" came between. So
  a genuinely new set with identical numbers typed on that line folds into the
  copy, as it already did before #541. Deciding that the page held the row uses
  `_client_held`'s test (same slot and values), so the link also shares that
  test's stale-tab limit from the 5a review. A later save carries the link to
  its new copy only when the posted row restates it unchanged, and an edit drops
  it. The logger can still clear or edit the copy like any structured row, and
  the admin shows the link read-only.
  **Only when the line isn't showing a set of its own.** If the athlete puts a
  different set on the line and later corrects it to the copy's values, that's
  an edit of the set on the line. It gets its own row and the copy stays, so a
  later clear of the line can't take the copy with it. The older `source_line`
  lookup keeps its behavior: its match already sits on the line, and declining
  it would leave two identical rows there, both hidden and both deleted by one
  clear.
  **Undo keeps a line that a copy points at,** the same way it keeps one a
  parsed row points at (`restore_plan_snapshot`'s stray-cell delete), so an undo
  past the line's creation can't cut the link.
  Not covered: copies made before this shipped have no link. A coach undo that
  puts the text back, rather than the athlete retyping it, still shows the set
  twice and tints the line "not logged as a set". The coach path never touches
  `LoggedSet`, and the data holds one row (#561, fixed on the read side below).
- 2026-09-19 — **Client beacon and push notification ledger (#509, third slice).**
  `POST /meso/api/track/` records the three moments only the browser knows
  (`pwa_installed`, `push_permission`, `push_clicked`) behind login, CSRF, a
  512-byte body cap and a per-user hourly rate limit, accepting a closed set of
  names and closed sets of prop values — a page chooses among values we named,
  it never computes one. Anonymous posts get a 204 and record nothing, which
  settles #542's open question about the beacon and anonymous actors. Accepted
  posts are `track(..., source=client)`, so the sandbox and staff exclusion
  applies unchanged; `track()` gained a keyword-only `source` for it.
  `notifications.PushNotification` is one row per push per subscription,
  dedicated rather than a generic `Notification(channel=…)` because the email
  half is already two tables shaped around SES's event stream. The row opens
  before the send, since its id rides in that device's payload URL; clicks are
  recorded by the owner's GET of the landing page (`?n=<id>` → a conditional
  UPDATE on `clicked_at IS NULL`, then the parameter is stripped with
  `history.replaceState`), not from the service worker, which has no CSRF token
  and no session to trust. Every ledger write is best-effort in its own
  savepoint. The dashboard gains a Push table (sent / failed / clicked per
  kind) beside Email, and "App installed" and "Push permission granted" feature
  rows whose "who" is *anyone* — the beacon fires from the athlete surface,
  which a self-coaching coach uses too, and carries no subject. Push rows ride
  the existing 13-month sweep. Migration `notifications.0003`.
  `PWA_CACHE_VERSION` → `meso-pwa-v5`, because putting a per-send parameter in
  the notification's URL broke three `sw.js` assumptions at once (see the push
  ledger section). Not in this slice: designer feature beacons, the in-app
  toast, migrating `TourEvent`, the GA property ids, and the privacy-page line.
  The adversarial review before merge changed four decisions: `push_clicked`
  left the beacon's accepted set, the beacon became JSON-only (a multipart post
  made CSRF drain the stream and `request.body` 500), the rate limit moved
  ahead of the other checks so it counts every attempt, and install reporting
  learned to wait for the beacon to land and to stay silent when storage can't
  remember. One finding was declined: a sandbox coach's push counting as a real
  send after the reaper nulls the user — `_notify_athlete_block_delivered`
  returns before notifying for a sandbox coach, and that is the only path to
  `_fan_out`, so the row can't exist.
- 2026-09-19 — **Fixed (#561): a coach undo after "Log session" shows the
  reclaimed set once.** The leftover from #541. The athlete types `225 x 5` on
  sub-line 1, the coach rewrites that line, the athlete taps "Log session"
  (which replaces the parsed row with a source-less copy carrying
  `reclaimed_line`), and the coach then undoes the rewrite. The line reads
  `225 x 5` again, so the athlete saw that performance twice — on the line and
  as a filled Set row — and the line was tinted "not logged as a set". One
  `LoggedSet` the whole time; only the page was wrong.
  **Fixed on the read side.** A coach undo must never write athlete data and a
  GET must not write, so nothing re-links here; the display predicates learn to
  read the link "Log session" already recorded. `parsed_set_is_hidden` now
  answers for a source-less copy too: the line named by `reclaimed_line` is
  showing it when that line's text parses back to its values.
  `sub_line_should_warn` and its own lookup follow, so the line isn't tinted.
  No new field, no migration.
  **A sub-line displays one performance.** New `line_displays(line, rows)` ranks
  the rows a line's text could be showing — its own parsed row first, then the
  older copy — and only the winner is hidden. Without the ranking, #541's
  "correction on a line that was showing a set of its own" ends with a parsed
  row and a copy holding the same values, and hiding both dropped a real logger
  row off the page. The `source_line` branch returns before the ranking, so the
  5a rule for parsed rows is unchanged (`_upsert_parsed_set` already refuses to
  mint a same-valued twin on one line).
  **The write side keys on the same predicate, so three places moved with it.**
  The replace-delete computes hidden-ness over the whole log at once (the
  ranking isn't answerable row by row) and so spares the copy. The twin absorb
  now also covers a surviving row hidden through `reclaimed_line`, recomputed
  after the delete — without it, a page loaded before the undo re-posts the Set
  row it still shows and logs the performance a second time. And the
  collision renumbering covers a hidden copy, which otherwise keeps the set
  number the athlete's now-empty Set row 1 posts into; a later reclaim makes
  both visible at one number and a single save can then delete both while
  reposting one. `hidden_parsed_set_pks` is the set-wise form both sides use.
  Not covered, still: a copy made before #541 shipped has no link, so a coach
  undo of its reclaim shows it twice exactly as before. And the logger still
  decides what a posted row MEANS by matching `(prescription, set_number,
  values)`, which a hidden row's number cannot carry reliably — a tab left open
  across the undo can re-create the performance as a second row if the copy was
  renumbered in between, and a genuinely new set with the same numbers, typed
  into the Set row the hidden copy invisibly occupies, is read as a restatement
  and dropped. Both were reproduced on `main` with an ordinary hidden parsed row
  and no `reclaimed_line` at all, so this fix gives a pre-existing defect a
  second trigger rather than creating it; the payload cannot tell the two
  meanings apart, and the real fix is to post each row's id back (#567). The
  warn lookup's own database fallback is also unscoped by log, so a set from
  another session can clear a line's tint (#568).
- 2026-09-19 — **Fixed (#567, #568): "Log session" identifies rows by id, not
  by set number.** Both failures were reproduced on `main` first, with an
  ordinary hidden parsed row and no `reclaimed_line` involved. A hidden row
  occupies a set number whose Set row renders empty, so the athlete's second
  set of the same weight and reps, typed into that empty-looking row, was read
  as a restatement of the hidden row and dropped — nothing created, no sets in
  the response, and retyping could never persist it. And a stale tab reposting
  its unchanged payload after renumbering had moved the hidden row missed the
  absorb, so one performance became two rows. #561 gave the second failure a
  further way in; neither was new.
  **Row identity in the payload.** Each posted set now carries exactly one of
  `id` (the `LoggedSet.pk` the client rendered in that grid row, handed out by
  `serialize_session_log` and now also by the presenter's `set_rows`) or
  `client_id` (a client-minted id, at most 64 characters, for a grid row with
  no server row yet). A payload that carries both on one set, or that mixes
  tagged and untagged sets, is a 400 — so `identified` is a validated property
  of the whole payload, not an inferred one. `client_id` was chosen over an
  explicit "this row is new" flag because it also names the row across a
  retry: `save()` builds the payload twice and the offline outbox can replay
  either copy, and a remembered client id keeps all three naming one row. The
  server echoes the mapping back on the row it created
  (`log.sets[].client_id`), so the page learns the real id without a reload.
  **What an id is worth, in four cases.** The rule is the same at all three
  match sites — the replace-delete's `_client_held`, the twin absorb, and
  #541's carried reclaim link:
  an **anchored** id (it names a row in `live_pks`, the snapshot of this log's
  rows taken before this save deletes anything) matches on the pk, and must
  agree on the prescription;
  a **stale** id (tagged, but names no row this log still holds) falls back to
  the positional `(prescription, set_number)` + values match — a stale id is
  not weaker evidence, it is no evidence, and position is all that is left;
  a **`client_id`** names no server row by construction, so it holds nothing,
  absorbs nothing and claims no link — which is what stops the swallowed set;
  an **untagged** payload is positional everywhere, byte-for-byte as before.
  The stale case is the one the review had to teach us: the write-ahead outbox
  replays a body whose first delivery committed but whose response was lost,
  and that body names rows the first delivery already replaced under new pks.
  Treating those ids as "no match" duplicated a performance and dropped
  #541's link — worse than the guess it replaced, and a direct contradiction
  of the client's own "replaying on reconnect is safe".
  **The rules the identity match makes expressible**, which position could
  not: a restated row is the same row; a new row with the same numbers is a
  new performance; renumbering still prevents two rows at one number.
  `_client_held` drops the value check on an anchored id — the id is stronger
  evidence than the values ever were — so an edit to a visible parsed row now
  REPLACES it instead of being spared into a visible duplicate. That is a
  deliberate change of the 5a rule, and only on the anchored path. One
  exception, because an id can be adopted rather than rendered: a wholly blank
  posted set is not evidence the page was showing a row's values, so it never
  holds a row that has any. `rowFilled` posts a row that is merely ticked, and
  without that guard an all-empty row deleted a logged set and left nothing.
  **Old payloads still work.** An installed PWA can run cached JS and a tab
  open since before the deploy posts no ids at all, so an untagged payload
  falls back to the positional match byte-for-byte. On that path both failures
  above remain exactly as reachable as they are on `main` — a client that
  cannot name its rows cannot be told apart from one merely re-describing a
  row it can already see — which is why `PWA_CACHE_VERSION` goes to
  `meso-pwa-v6`, so installed clients drop the stale shell and pick up the new
  logger. The client reconciles by `client_id` first and slot second, rather
  than by `client_id` alone: during a rolling deploy an older container
  ignores `client_id` and echoes none, and matching only on it left the row
  it had just stored un-ticked, out of the next payload, and deleted. A stale
  tab's save also still deletes a set logged from another device between its
  render and its post; that is the wholesale replace, not the identity match,
  and it is out of scope here.
  **#568, one rule and one scope.** `sub_line_should_warn`'s fallback query
  matched rows across every `SessionLog` in the database while its twin
  `parsed_set_is_hidden` scopes by log, so the blur response and the next page
  render could disagree. `_cell_warn_or_false` now reads the same newest log
  the presenter reads and passes `backing_sets`. It stays OUTSIDE
  `athlete_cell_write`'s transaction, where `main` had it: an attempt to move
  it inside under a savepoint, by analogy with `_upsert_parsed_set`'s guard,
  turned a database failure in that read into a silent full rollback returned
  as a 200 — the athlete's committed text gone while the client recorded a
  save. Reading after commit is what keeps a swallowed failure away from a
  write that already succeeded. The four reads that answer "the athlete's
  newest log for this session" now share a `-pk` tie-break so they cannot pick
  different logs. The fallback query itself is scoped to the cell's own day as
  a safety net; a cell names no athlete, so it can never narrow further, and
  both real callers pass `backing_sets`.
  **The move case, decided:** after a coach moves an exercise to another day,
  a line whose set was logged on the old day reads as **unlogged** on the new
  one. The cell travels with the `ExerciseSlot` and the `LoggedSet` stays
  behind, so the scoped answer is the true one, and it is a behavior change
  for ordinary parsed rows too, not only `reclaimed_line` copies.
  **A consequence the round-2 review surfaced, not fixed here.**
  `prescription_move` re-points the `ExerciseSlot` block-wide, so one
  cross-day drag tints every already-logged sub-line for that exercise in
  *every week of the block*, not just the one week the coach dragged — the
  move case above is really "one drag, many now-unlogged lines". And because
  the client re-posts a warned line on blur (`_lineNeedsSending`), an athlete
  who focuses and leaves one of those newly-tinted lines mints a SECOND
  `LoggedSet` on the new day's log while the old day's row stays put — two
  rows for one performance, on two different days. Both are pre-existing (the
  presenter already tinted those lines on `main`, before #567/#568 touched
  any of this) and are tracked separately, not fixed in this slice.
  No model change, no migration.
- 2026-09-20 — **Fixed (#570): the collision-renumbering walk now has a
  ceiling, and refuses honestly when nothing is free.** Found by the
  adversarial review on #567/#568 and declined there as pre-existing. The
  walk that moves a surviving parsed row off a set number the client just
  claimed used to be a bare `number += 1` with no upper bound, while
  `_clean_logged_sets` rejected any posted `set_number` above
  `MAX_LOGGED_SET_NUMBER` (50) and the presenter's `_set_rows` rendered up to
  a separate `hard_cap=60` — three numbers that didn't agree with each other.
  A survivor could climb past 50, the presenter would still draw it as an
  ordinary fillable Set row, and the moment the athlete filled or ticked it
  the endpoint rejected that number and 400'd the WHOLE payload — a hard
  lockout, since the athlete couldn't save anything else in that session
  either. Reaching it takes roughly 50 rows on one prescription, so it's not
  a likely accident, but the three constants disagreeing was a bug regardless
  of how rarely it bites.
  **The three constants now agree.** `MAX_LOGGED_SET_NUMBER` moved out of
  `views.py` into `models.py`, next to `LoggedSet` (it's really a property of
  that model), because `presenters.py` cannot import from `views.py` — the
  reverse import already exists. `views.py` re-exports it
  (`from .models import MAX_LOGGED_SET_NUMBER`) so nothing that already spells
  it `views.MAX_LOGGED_SET_NUMBER` had to change. `presenters._set_rows`'s
  `hard_cap` now defaults to that same constant instead of a hand-picked 60,
  so the grid can never render a row the endpoint would go on to reject.
  **The walk is bounded, not just capped.** `_first_free_set_number` searches
  upward from the row's own number to the ceiling first — preserving the
  existing "fall through to the next free number" behavior — and only when
  nothing is free there does it scan the whole `1..MAX_LOGGED_SET_NUMBER`
  range from the bottom, so a hole an earlier step in the same save already
  opened up is still found rather than needlessly refusing.
  **No free number left means the save is refused, not corrupted.** When the
  legal range is genuinely full, `athlete_log_session` calls
  `transaction.set_rollback(True)` and returns an `HttpResponseBadRequest`
  naming the exercise (`f"Too many sets logged for {name}."`, `name` off the
  line-0 `Prescription` the row's `prescription` points at). The
  `set_rollback` is load-bearing, not decorative: this save's own
  `log.sets.filter(pk__in=replaceable).delete()` has already run inside the
  same `atomic()` block, so returning the response without it would commit
  those deletes and refuse the save anyway — the athlete's rows gone AND the
  save rejected. Marked for rollback, the block's exit undoes everything, so a
  refused save writes nothing at all, and the collision is never left in place
  for a later save to find two rows on one number. (`set_rollback` rather than
  raising a private exception so the whole 400-line block doesn't have to move
  a level deeper inside a `try` for a three-line guard.)
  **A fourth writer, found by the adversarial review.** `_upsert_parsed_set`
  had the same unbounded walk (`number = cell.line; while number in taken:
  number += 1`), so the "three constants now agree" claim above was false as
  written: an exercise already carrying the full legal range could still mint
  a row at 51+ through a sub-line blur. It uses `_first_free_set_number` too
  now, and when nothing is free it simply creates no row. Nothing is lost by
  that — the rows a blur replaces are deleted before the number is chosen, so
  a line that HAD a row always finds its own number free again, and `None`
  can only mean this line never had one. The cell's text is saved either way
  and the line reports itself unlogged, which is exactly true.
  **Lowering the presenter's cap needed a matching skip in the
  replace-delete.** Dropping `hard_cap` 60 -> 50 means a row already sitting
  at 51..60 (left by either unbounded walk) stops rendering — and a row the
  logger doesn't render is one the client can't repost, so `athlete_log_session`
  would have swept it into `replaceable` and deleted it silently on the next
  save. That trades a hard lockout for losing a set, which is the worse half
  of the trade. The `replaceable` loop now skips any row above the cap, for
  the same reason it already skips non-trainable and hidden rows: it is
  history, not draft state. No backfill migration, because nothing is
  destroyed and such a row still counts everywhere it did before.
  **The bound needed a fallback, found by re-attacking it.** The first
  version of the `_upsert_parsed_set` bound simply created nothing when no
  number was free, on the reasoning — written into a comment, and wrong —
  that "a line that HAD a row always finds its own number free again, so
  `None` can only mean this line never had one". False for exactly the rows
  #570 is about: the blur deletes the row this line was showing BEFORE the
  number is chosen, so a row left at 51+ by the old unbounded walk frees a
  number outside the range the scan covers. The helper answered "nothing
  free" while this line's own slot sat right there, and an ordinary edit
  deleted a performed set and put nothing back — on a 200 response, which is
  worse than the out-of-range row the bound was introduced to prevent. The
  numbers the blur frees are now captured before the delete and used as a
  fallback, re-checked against `taken` because a history restore can leave a
  freed number belonging to a different line-0 cell entirely.
  **Sparing an out-of-range row is not repairing it.** A row past the ceiling
  whose `source_line` still names a live sub-line comes back into range the
  next time that line is edited. A source-less `reclaimed_line` copy has no
  such path — the renumbering loop can never see it either, since its slot
  can never appear in `posted` — so it stays where it is, invisible on the
  page and still counting toward 1RM and PRs. The skip preserves that state
  rather than fixing it, which is the right trade against deleting it, but it
  is a state and not a repair.
  **Round 3: `mine` is scoped by prescription too.** The freed-number
  fallback still had a path to a net deletion, and the branch had briefly
  pinned it as a TEST rather than a bug. `mine` — the rows a blur replaces —
  filtered on `source_line` alone, so a row pointing at this sub-line while
  belonging to a DIFFERENT exercise was deleted by a blur on this one; `taken`
  is scoped to THIS prescription, so that row's freed number said nothing
  about where the replacement could go, and with the legal range full nothing
  was created. One fewer performance, on a 200. `mine` now requires
  `prescription=line_zero_cell` as well, so such a row is never a candidate
  for this delete — sparing a row this path cannot account for, the same call
  the replace-delete's own trainable/hidden skips make. The freed-number
  re-check against `taken` stays: it is the one place a number is decided, and
  a freed number is only free while nothing else has taken it.
  **400 here, 503 for #571 — deliberately different.** This refusal is a 400,
  which `meso_athlete.js` classifies as "rejected" and drops from the outbox;
  #571's poisoned-transaction answer is a 503, which it keeps queued and
  retries. The difference is whether retrying can help: a save with no free
  set number is refused deterministically and will be refused identically
  next time, while a poisoned connection is a transient the next attempt may
  well get past. The athlete sees the server's own message naming the
  exercise, so a refusal that cannot be retried at least says why.
  **That message is a JSON `error` field, not the response body.** The client
  renders it to the athlete verbatim, so only a refusal written FOR them may
  carry it. This endpoint's other 400s — `_clean_logged_sets`'s "Duplicate id
  in sets.", "status must be 'pending' or 'done'." — are developer-facing and
  stay bare `HttpResponseBadRequest` text, which is exactly how the client
  tells the two apart. An earlier version guessed from the body instead (drop
  it if it is long or looks like HTML), which both let every validation
  message through and would silently drop a legitimate one once an exercise
  name made it long enough. Opting in per message beats sniffing the shape.
  **Two sibling claims the review caught in the same client.** `save()` sets
  the status to "done" optimistically; restoring it on a refusal fixed one
  half and left the other — when `localStorage` refuses the write-ahead
  entry, NOTHING holds the save, not the server and not the outbox, and the
  badge still read "Logged" with no retry pending. `keepForLater` now reports
  whether it kept anything, and the status goes back when it didn't. And
  `reportSaved()` no longer claims "Saved ✓" at all while a refusal stands.
  **Round 3 corrected both of those again.** Clearing the refusal to make room
  for the tick — round 2's version — states the wrong thing more confidently:
  a refused save drops its own outbox entry, so nothing retries it, and the
  flush that reaches `reportSaved` may have landed a log queued by ANOTHER tab
  on the same session (`flushedMine` means a log for this URL landed, not that
  this page's did). A refusal now outranks a tick, and `save()` clears it at
  the top of the next real attempt — the moment it stops being true. Round 3
  also found the refusal contract stopped at `save()`: `flushLog` returned
  "kept" for EVERY non-ok answer, so a refusal replayed from the outbox was
  re-POSTed on every `online` event, forever, behind a footer promising it
  would sync — and the comment added beside the 400 claiming the client drops
  such an entry was therefore false. `flushLog` now makes the same
  retryable/refusal split `flushCell` always had, keeping another session's
  refused log queued for `flushCell`'s own reason: this page has nothing on
  screen to report it on. And `keepForLater` returning false no longer reverts
  the status when an earlier write-ahead copy of the same save is still in the
  outbox — `writeQueue` is all-or-nothing, so a later `enqueue` can fail while
  that copy sits there due to flush.
  **And one more the verification pass caught.** Splitting `flushLog` by
  status dropped 403/409 with the rest, where `flushCell` returns "offline"
  for those FIRST — they mean "not postable as this account right now" (a
  rotated CSRF after a re-login; this page captures `csrf` once, at load), not
  "refused". Dropping one destroys the only copy of a session logged offline,
  since a queued log's set rows — unlike a sub-line's text — are never
  restored into the grid on load. The guard is back, ahead of the split. The
  same pass noted that a dropped log left the optimistic "Logged" badge up,
  the very claim `save()`'s revert exists to stop, just reached via the flush:
  `save()` now records what the status was before it queued, and the flush
  refusal puts it back (only when it knows — an entry from a previous page
  load carries none, and the next load reads the server anyway).
- 2026-09-20 — **Fixed (#571): `athlete_cell_write` no longer claims a save
  the database didn't keep.** Found by the same adversarial review as #570.
  `_upsert_parsed_set` wraps its work in a nested savepoint and swallows
  every exception, on the reasoning that a parse or DB problem inside it must
  never turn a blur into a 4xx/5xx or lose the athlete's already-committed
  cell text (parse-at-commit plan §11). That reasoning holds for a failure
  *raised inside* the savepoint — an ordinary single-statement failure
  recovers fine through `ROLLBACK TO SAVEPOINT`, even from an aborted
  transaction. It does not hold when the savepoint's OWN rollback also fails
  — a dropped connection, a pgbouncer `server_lifetime` cycle, a mid-request
  DB restart — which is exactly when Django leaves `connection.needs_rollback`
  set in a way no enclosing savepoint absorbs. Two shapes followed, depending
  on what ran next: **(1)** a query following the swallow inside the same
  outer atomic — the `_line_sets(...)` re-read or the `SessionLog...update()`
  activity bump — raised `TransactionManagementError`, which escaped as a 500
  AND took the outer atomic's already-saved `cell.save()` down with it, both
  halves of the tolerance guarantee broken at once; **(2)** nothing following
  — the outer atomic exited with no exception and `needs_rollback` still set,
  so it rolled back SILENTLY and the view answered 200 with the text echoed
  back, which `meso_athlete.js` records as a landed save (drops the
  offline-queue copy, sets `savedText`) — so nothing ever retried and the
  write was simply gone.
  **The check has to live inside the `with transaction.atomic():` block, not
  after it.** `Atomic.__exit__` clears `connection.needs_rollback` on its own
  way out, so a check placed after the block always reads an already-cleared
  flag and can never see this. `athlete_cell_write` now captures
  `poisoned = connection.needs_rollback` as the LAST statement inside the
  block — a plain attribute read, not a query, so it's safe even against a
  poisoned connection — and answers off `poisoned` once the block has exited.
  A GUARD ahead of that capture closes shape 1: the activity-bump `if` now
  checks `not connection.needs_rollback` FIRST in its `and` chain, so a
  poisoned connection short-circuits the whole condition before either
  `_line_sets(...)` or the `.update()` — both queries — ever runs, instead of
  raising `TransactionManagementError` into the block. (If the connection is
  genuinely dead rather than merely marked, the block's own exit can still
  raise on its real rollback — a 500, which is honest in the same direction
  as the fix below: the client treats any 5xx as "kept, not lost.")
  **A poisoned response is a 503, not a 4xx.** `meso_athlete.js`'s
  `isRetryableStatus` treats `status >= 500` (plus 408/429) as outcome
  `"kept"` — the server failed, not the write — which leaves the cell's
  outbox entry in place and clears `entry.savedText` so the next blur
  reposts it; a 4xx reads as `"rejected"`, dropped from the outbox and never
  retried, which is exactly wrong for a write the database never actually
  kept. `athlete_cell_write` returns `{"ok": false, ...}` with status 503 in
  this one case, so the client's existing retry path — already exercised for
  ordinary 5xx failures — does the recovery instead of a new mechanism.
  **`ATOMIC_REQUESTS = True` deleted, not moved.** It sat at MODULE scope in
  `config/settings/base.py`, outside `DATABASES["default"]` — not where
  Django reads it (`BaseHandler.make_view_atomic` checks
  `settings_dict["ATOMIC_REQUESTS"]` per database alias) — so it was inert
  and had been since it was written; #567/#568's investigation found it but
  left it alone pending this fix. Moving it into `DATABASES["default"]`
  would wrap every view in a request-level transaction and re-arm this whole
  class of bug for every swallowed failure anywhere in the request, not only
  this one now-fixed call site — a deliberate change that needs every other
  swallowed-failure pattern in the app found and handled first, which this
  slice does not do. The line is deleted with a comment explaining why,
  `_cell_warn_reason_or_blank`'s own P2-B docstring paragraph is updated to match
  (the setting is gone, not merely relocated, and #571's fix does not cover
  that function's own post-commit read — it runs after
  `athlete_cell_write`'s block has already exited, so it would sit inside a
  *different*, outer, request-level atomic that this fix never sees). No
  test pins `ATOMIC_REQUESTS` off, on the same reasoning #567/#568 gave: that
  would freeze an accidental, currently-harmless absence as though it were a
  deliberate contract.
- 2026-09-20 — **Fixed (#572 part 2): a warned line now carries WHY, not just
  whether, so the client can tell a repair from a duplicate.**
  `_lineNeedsSending` (`meso_athlete.js`) has always reposted a warned line
  whose text hasn't changed — that's the un-skip repair: set-shaped text
  typed while a row was skipped saves no set, and re-sending the same text
  once the coach un-skips the row is how it gets one. #568's day-scoping put
  a second case behind the same `warn: true` with the opposite correct
  answer: after a cross-day move, a line whose set is logged on the OLD day
  reads tinted on the new one (the cell travels with the block-shared
  `ExerciseSlot`; the `LoggedSet` stays on the old day's log — #568's
  decision, and it stands; see below). Reposting THAT line isn't a repair —
  the performance already exists — it runs `_upsert_parsed_set` against the
  new day's log and mints a SECOND row for one performance, while the old one
  stays, hidden but still counting toward results, 1RM and the agent's
  grounding. A bare boolean cannot tell the two cases apart, so
  `parsing.cell_warn_reason` and `models.sub_line_warn_reason` now return the
  REASON — `None` / `"unresolved"` / `"skipped"` / `"too-long"` /
  `"unlogged"` / `"elsewhere"` — and `_lineNeedsSending`'s gate became
  `entry.warn && entry.warn_reason !== "elsewhere"`: every reason still
  reposts except this one.
  **"elsewhere" vs "unlogged" is decided by `line_displays`, not by whether a
  row exists anywhere.** `sub_line_warn_reason` takes an `elsewhere_sets`
  argument — the athlete's rows for this cell on any OTHER session — and only
  answers `"elsewhere"` when `line_displays(cell, elsewhere_sets)` finds one
  of them still SHOWING this cell's exact text, the same one-row-per-line
  ranking `parsed_set_is_hidden` uses everywhere else in this slice. A row
  that merely exists on another day but no longer matches the cell's current
  text — the athlete has since edited it — doesn't count: reposting would
  create a NEW performance, not duplicate an old one, so `"unlogged"` is the
  honest answer and the client goes ahead and sends it.
  **A caller that never passes `elsewhere_sets` gets `"unlogged"`, on
  purpose.** Both real callers (`athlete_session`, the renamed
  `_cell_warn_reason_or_blank`) pass it — one query per session, batched the
  same way `sets_by_line` already is — but the fallback for a caller that
  forgets is the reason that keeps the un-skip repost firing, not the one
  that would silently start swallowing sets. Between an occasional needless
  repost (idempotent) and an occasional missing one (a lost set), the second
  is the one worth being wrong in the direction of.
  **`""` from an older server, mid rolling deploy, also keeps today's
  behavior.** The client reads `data.cell.warn_reason || ""`, so a server
  that hasn't shipped this change yet — or a stale service-worker cache —
  sends no `warn_reason` at all, and `"" !== "elsewhere"` is true: the line
  reposts exactly as it always has. Only a server that actively answers
  `"elsewhere"` changes the client's behavior, so the two halves of a rolling
  deploy can never disagree in the dangerous direction; that's why `""` is
  the safe default rather than, say, refusing to repost until told otherwise.
  **Renamed, not duplicated:** `views._cell_warn_or_false` is now
  `_cell_warn_reason_or_blank` (returns the reason string or `""`, not a
  bool), and `models.sub_line_should_warn` is now `sub_line_warn_reason`
  (returns the reason or `None`); `parsing.cell_warn_reason` replaces
  `cell_should_warn`, which the adversarial review found had no callers left
  once every surface wanted the reason. `PWA_CACHE_VERSION` bumped to `meso-pwa-v7` so an
  already-installed PWA picks up the new response shape rather than keep
  serving a cached one that never carries `warn_reason`.
  **What the client-side gate can and cannot enforce — narrowed by the
  adversarial review.** The suppression lives in `_lineNeedsSending`, which
  only governs the blur path, so it closes the case the issue describes: a
  line tinted by a move, focused and left with its text UNCHANGED, no longer
  posts. It does not close every route to a second row, and the first draft
  of this entry claimed it did. `_postCell`'s `fromQueue` branch never
  consults `_lineNeedsSending` at all — deliberately, because #527's outbox
  must drain — so an offline edit-then-revert on an `"elsewhere"` line leaves
  a queued entry whose text is byte-identical to what the server already
  holds, and the replay mints the second row. So does any forced post: a
  trailing-space edit is enough. Neither is a regression (on `main` the
  plainer focus-and-leave path did the same), and the obvious server-side
  cure is worse than the disease — refusing to create a row whose text is
  backed on another day would also refuse a genuine RE-performance of the
  same numbers on the new day, which is real data loss. The residue is filed
  as its own follow-up rather than patched here.
  **`elsewhere_sets` reads ANY log of the other session, unlike
  `backing_sets`.** #568 deliberately pinned `backing_sets` to the newest
  `SessionLog` (`-created_at`, `-pk`); this read deliberately does not, and
  the asymmetry is not an oversight. A row stranded on a split or older log —
  or on a day the coach has since soft-deleted — is invisible on every
  athlete surface, but `one_rm.derive_one_rm_values` and
  `personal_records._live_logged_sets` filter by neither log recency nor
  `deleted_at`, so it still counts toward the athlete's 1RM and PRs. A repost
  would therefore still genuinely double-count it, which makes `"elsewhere"`
  the true answer. A review round proposed adding
  `session_log__session__deleted_at__isnull=True` to both reads; that would
  have turned a correct suppression into the very duplicate this slice
  exists to prevent. (That those rows count at all is a separate,
  pre-existing bug, filed on its own.)
  **Part 1 of #572 is explicitly NOT fixed here.** `prescription_move`
  re-points `exercise_slot.session_slot`, and an `ExerciseSlot` is shared
  across every week of the mesocycle, so one cross-day drag still re-tints
  every already-logged week of the block, not just the week the coach was
  looking at — an athlete opening an old, completed week still sees "not
  logged as a set" on work they already logged. Whether a move should
  instead carry the athlete's `LoggedSet` rows along with the slot, or scope
  the tint to one week some other way, is left open on #572 as a product
  decision this slice does not make by default.
- 2026-09-20 — **Fixed (#583, #584): a coach redo/undo can no longer collide
  with a live coordinate, and the stray-cell purge now locks before it
  deletes.** Both found by the adversarial review on the #577 branch (PR
  #582), both confirmed to fire on `main` too — pre-existing, not caused by
  that PR.
  **#583 — the pk is an implementation detail of REVIVING a cell; the
  `(exercise_slot, week, line)` coordinate is what the cell actually IS.**
  `restore_plan_snapshot` revives a snapshotted cell by pk (deliberately — a
  redo has to put back the exact row a matching undo took away, not a
  lookalike with a new pk), but `Prescription` also enforces
  `unique_cell_slot_week_line` on that coordinate. The verified reproduction:
  a coach writes a sub-line (pk 3), undoes it (the stray-cell purge
  hard-deletes pk 3, since nothing else names it yet), the athlete then
  blurs a value onto the SAME sub-line — `athlete_cell_write`'s
  `get_or_create` mints a brand-new pk 4 at the coordinate the purge just
  freed, and (by design) records no `PlanAction`, so the pending redo entry
  still thinks pk 3 belongs there — and the coach's redo then tries to
  `save()` pk 3 straight onto a coordinate pk 4 now occupies, violating the
  unique constraint and 500ing the whole redo.
  **The fix resolves every coordinate collision BEFORE the cell upsert loop
  runs, applying the SAME rule the stray-cell purge already states for a
  cell absent from the snapshot — one join earlier, to a coordinate a
  REVIVED pk wants rather than only to a stray pk the purge would delete on
  its own.** An occupant that is athlete data — `athlete_authored`, or a
  cell some `LoggedSet` still points at through `prescription`/`source_line`/
  `reclaimed_line` (factored into one shared helper,
  `history._cells_athlete_data_points_at`, used by both this guard and the
  purge below) — KEEPS the coordinate, and the snapshotted cell that wanted
  it is skipped for this restore entirely. Otherwise the occupant is a
  coach-made row absent from the snapshot — exactly what the purge would
  remove anyway, just discovered here one step earlier — and it is deleted,
  freeing the coordinate for the revive.
  **The accepted cost is the same trade #577's `logged_sets` spare clause
  already makes: a coach's redo of that one line silently does nothing —
  the athlete's coordinate wins — rather than 500ing the whole redo.** A
  coach can retype a line; nobody can retype the athlete's performance. This
  is consistent with the root shape #578 is reviewing (one cell, two writers
  with different history semantics) rather than a new precedent: wherever
  the two collide, the athlete's data has been the one that survives since
  #577, and this extends that same rule from "cell some athlete data points
  at" to "coordinate a revived pk wants but athlete data already occupies."
  **A genuine within-snapshot SWAP (two snapshotted cells trading
  coordinates in one restore) is deliberately left unresolved** —
  `unique_cell_slot_week_line` is checked immediately, not deferred, so
  neither side can be written into the other's still-occupied coordinate
  without first parking one of them on a temporary, unclaimed coordinate,
  and no known real path produces this shape today. It still raises
  `IntegrityError` out of the upsert loop exactly as before this fix; see
  `test_restore_cell_collision.py::TestSnapshotSwapIsNotHandled`, which pins
  that this is a conscious non-fix, not an oversight.
  **#584 — the stray-cell purge qualified its candidates without a row
  lock, so a `LoggedSet` committed between the qualifying SELECT and the
  DELETE was invisible to all three spare checks and the cell was deleted
  anyway.** `QuerySet.delete()` is SELECT-then-DELETE with no lock of its
  own. For `prescription`/`source_line` (real FKs) that surfaced as a
  COMMIT-time deferred constraint violation — a 500 on the coach's undo; for
  `reclaimed_line` (`db_constraint=False`, #541) nothing stopped it and the
  hint was left dangling. Fixed by qualifying the purge's candidates under
  `select_for_update(of=("self",))` and re-checking every spare test AFTER
  the lock is held, not before — `settle.settle_log` already uses this exact
  `of=("self",)` shape, for the same reason (the queryset joins through to
  `Plan`, and without `of` Postgres would lock those joined rows too).
  **`athlete_authored` stays in the candidate filter and is re-checked under
  the lock too.** It cannot be moved out: an athlete-authored cell is never
  captured in a snapshot, so it is never in `cell_pks`, and dropping the
  exclusion would make every athlete-authored cell of the plan a candidate —
  this `FOR UPDATE` would lock all of them on every undo and redo. Those are
  exactly the rows a logging athlete writes, and `athlete_cell_write` takes
  its locks the other way round (sub-line `Prescription`, then `Plan`) while
  a restore holds `Plan` throughout and reaches `Prescription` last, so that
  would have manufactured a routine deadlock out of a fix for a different
  race. Nothing is lost by keeping it: the flip the re-check exists for is a
  concurrent UPDATE of one of these rows, and under READ COMMITTED a
  `SELECT ... FOR UPDATE` that waits on such a row re-evaluates its own
  WHERE clause against the updated version and drops it (PostgreSQL's
  EvalPlanQual recheck, docs §13.2). The re-check repeats it anyway, so
  "what spares a cell" is answered in one place, once the rows can no longer
  move.
  **The serialization contract was strengthened after #584.** A first draft
  of this entry claimed a `LoggedSet` insert holds `FOR KEY SHARE` on the
  `Prescription` it references for the rest of its transaction. That is
  false: Django emits these FKs `DEFERRABLE INITIALLY DEFERRED`, so the
  constraint lock fires only at COMMIT. At #584's landing, the unconditional
  `Session` lock/save on both sides supplied the real serialization. Since
  #588, every athlete write takes the `Plan` mutex first and then
  `Session ... FOR UPDATE OF self`, so Plan is now the documented outer
  boundary and the Session lock remains the log-level boundary.
  **The lock is still worth taking, for a narrower reason:** it stops the
  purge depending on an incidental UPDATE in an unrelated earlier loop.
  Make that `session.save()` conditional — skip unchanged rows, a plausible
  optimization — and the race reopens with nothing to catch it. It also
  covers a writer that touches one of these cells without holding the plan's
  Session rows. `test_undo_purge_postgres.py` now pins both halves
  separately: one forced-interleaving test for this lock in isolation (its
  athlete thread deliberately does NOT take the Session lock; fails on
  `main`), and one characterization test for the Session-lock serialization
  (passes on `main` too, and exists so that a future change making the
  Session save conditional fails there instead of silently).
  **Plain `FOR UPDATE`, not `no_key=True`,** for two reasons: a DELETE takes
  a lock of that strength anyway, and `FOR UPDATE` is what conflicts with
  the commit-time `FOR KEY SHARE`, so a writer already *inside* its COMMIT
  makes this lock wait and the re-check then sees its row. #560's
  `no_key=True` was right for a user-row mutex never contending with an FK
  reference; this is the opposite case.
  **Lock order is unchanged; the locked SET is wider.** The sequence is
  still Plan → Week → SessionSlot → ExerciseSlot → Session → Prescription.
  But the `.delete()` this replaces carried its spare clauses inside its own
  qualifying SELECT, so it only ever locked the *doomed* rows, whereas this
  locks every snapshot-absent, non-athlete-authored stray and then spares
  some. At #584's landing that widened the then-existing
  Prescription↔Plan cycle. #562 subsequently moved `athlete_cell_write`'s
  Plan lock ahead of Session/Prescription, closing that inversion; keeping
  `athlete_authored` in the candidate filter still avoids taking needless
  locks on the athlete's rows.
  **The collision-resolution delete above (#583) removes an occupant only
  when this purge itself would**, including the same soft-deleted-slot/week
  scoping AND the same split between what is filtered and what is
  re-checked, so the two halves cannot disagree about which cells are
  protected. Its occupancy read is unlocked; today the outer Plan mutex on
  every athlete write stops a concurrent occupant, rather than this lock.
  **The guard must never take `FOR UPDATE` on an athlete-authored occupant,
  and a review round caught it doing exactly that.** Both round-2 reviewers
  found it independently: the guard locked its candidates before testing
  them, which contradicted the purge's own argument one screen below for
  keeping `athlete_authored` in the FILTER. It is not theoretical —
  at the time, `cell_line_write`'s reclaim wrote that row before
  `record_plan_action` acquired Plan, making Prescription→Plan against a
  redo's Plan→Prescription. #562 later added the explicit Plan lock above
  that reclaim. The guard still decides the flag from the unlocked occupancy
  read and never locks athlete-authored occupants; only the `LoggedSet`
  pointers are re-checked under the lock.
  **A skipped cell is now logged.** Skipping is otherwise invisible — the
  endpoint answers `ok: true` and the line simply does not come back — so
  `history` logs the skipped pk, its coordinate and the occupant's pk. The
  message says only that the coordinate is taken, deliberately: three
  branches reach that skip and only two of them involve athlete data, and a
  skip is not necessarily permanent (a later restore whose snapshot has that
  slot and week live takes the stray-delete branch, and a coach reclaim
  through `cell_line_write` makes an athlete-authored occupant deletable
  again). Round 3 caught an earlier draft of that message asserting both the
  reason and the permanence, and being wrong on each.
  **`test_undo_purge_postgres.py` is in the Postgres CI job's file list.**
  That list is the only place these files run — each skips itself on SQLite,
  so one left off it executes in no job at all and looks green while testing
  nothing. Round 3 caught the new file missing from it; the list now carries
  a comment saying to add Postgres-only files in the commit that creates
  them.
  **Historical scope note:** #584 did not address #562. #562 later moved
  `athlete_cell_write` to Plan-first order, and #588 extended that same rule
  to `athlete_log_session` and `settle_log`; those later decisions supersede
  the Session-first assumptions recorded in the original #584 review.
  Tests: `test_restore_cell_collision.py` (the #583 reproduction through the
  real `api_cell_line_write` → `api_plan_undo` → `athlete_cell_write` →
  `api_plan_redo` sequence, an undo-leg counterpart constructed directly
  against a hand-built `PlanAction.snapshot` since no real endpoint sequence
  was found to reach it, the "stray with no athlete data is still purged"
  counter-case, and the swap-is-not-handled pin) and
  `test_undo_purge_postgres.py` (Postgres-only, a forced two-thread
  interleaving proving the lock, per the same barrier recipe
  `test_settle_postgres.py`/`test_billing_webhook_postgres.py` use).
  **Declined, with reasons (adversarial review, 5 angles, round 1).** (a) The
  coach's profile card: `_profile_results` picks its session by `-date`
  across sessions and then calls `session_results`, which now resolves the
  log by `-created_at`, so for a pair whose newest write carries an older
  workout date the card can describe a log `_profile_results` did not pick.
  That is the intended unification, not a regression — the card now agrees
  with the results screen it reuses and with the athlete's own page. (b) The
  PR callout: `personal_records._logged_before` defines the prior-best
  baseline by `date` first, so moving `session_results`' subject to the
  newest-by-`created_at` log can, on such a pair, announce a record already
  beaten by its sibling. Narrow and legacy-only (it needs two DONE logs for
  one `(session, athlete)` plus a date/created_at inversion), and *not* new
  behaviour in kind: `athlete_log_session` and `_upsert_parsed_set` already
  selected the subject that way on `main`, so the athlete's own toast has
  always behaved like this — this change aligns the coach with it. Fixing
  `_logged_before` is a PR-provenance product decision; filed on #578, which
  is already chartered to replace the hand-written orderings with one
  selector. (c) A within-snapshot coordinate swap or chain still raises:
  unreachable, because `restore_plan_snapshot` is the only code in the repo
  that ever assigns a cell's coordinate, so a snapshotted pk's coordinate is
  identical in every snapshot naming it. (d) `reclaimed_line` is protected
  by neither the lock nor an FK, and is safe today only by an incidental
  property of `_consume_carried_link` (every carried link names a cell some
  already-committed `LoggedSet` also names). Unverified and untested; noted
  on #578 rather than asserted here.
- 2026-09-20 — **Row-lock cleanup #587/#588/#589/#590/#596:** parent mutexes
  now default to `FOR NO KEY UPDATE`; all Plan mutexes and the two coach User
  mutexes in scope were swept to that strength. `athlete_log_session` and
  `settle_log` now take Plan before `Session ... FOR UPDATE OF self`, and a
  vanished session loses with 404/`False`. `clear_demo` joins every loader on
  the coach mutex before reading its demo-athlete set. Plan/link creators now
  reserve their parent rows, invite acceptance reserves both User parents
  ascending, and hard deletes in the four admins plus `merge_users` use shared
  top-down `lock_cascade_*` helpers. `plan_create`'s draft path takes the coach
  `User` before the link, because `clear_demo`'s new coach mutex would otherwise
  have made the old link → User order in `_reserve_plan_draft` a reachable
  deadlock (the codex review of this change caught that). The explicit
  exceptions remain the coach+demo-athlete bulk admin delete above, the unswept
  lock classes and creator entry points listed above, and #584's deliberately
  plain Prescription locks.
