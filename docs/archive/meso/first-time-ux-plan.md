# Meso — first-time UX / onboarding slice plan

**Status:** ✅ Slice complete · Q1–Q4 resolved 2026-06-29 · **Phase 1 (individual
plan creation) + Phase 2 (coach first-run: one-click demo + empty-state teaching)
+ Phase 3 (the front door: anon landing + main-site link) + Phase 4 (athlete
first-run: install prompt + first-log coachmark) + Phase 5 (designer & agent
self-explanation: dismissible coachmarks + review-gate note + real left-rail
chrome) all done**
**Companion to:** [`decisions.md`](../../meso/decisions.md) (B1 multi-coach, B2 athlete
login, N3 roles, N4 invites) · [`invites-plan.md`](./invites-plan.md) ·
[`athlete-plan.md`](./athlete-plan.md) · [`groups-plan.md`](./groups-plan.md)
**Goal of this slice:** make Meso **obvious to use for a first-time visitor**
across all three personas — the cold/anonymous visitor, the brand-new coach, and
the newly-invited athlete — and close the one structural gap that currently makes
the coach's happy path a dead end.

Every other Meso slice has been a *capability* slice (persistence, agent, athlete
PWA, groups, 1RM, invites). The feature area is now broad and deployed, but it has
never had an **onboarding** pass: nothing tells a newcomer what Meso is, routes
them to the right surface, or walks them from an empty account to a delivered
program. This slice is that pass.

---

## The current first-time experience (what we're fixing)

Three first-timers arrive at Meso, and the app serves them very unevenly. All
findings below are from the deployed code, with references.

> **Reconciled with `main` (2026-06-29, after #311–#316).** N4 Phase 2 (#311)
> changed the front-door routing: `RosterView` now sends **any non-coach** to
> `/meso/me/` (a user counts as a coach via a `CoachProfile` *or* a coach-side link
> *or* a sent invite), and the athlete home gained a "Your coaches" card with a
> **request-a-coach** form. That flips Persona 2's old "silently treated as a coach"
> gap into its inverse — a genuine new coach now has **no path *onto* the coach
> surface** — and means #311 already covers most of Phase 4's athlete-initiated
> item. Sections below reflect post-#311 behavior; the **headline blocker is
> unchanged**.

### 🚧 Headline blocker — a coach cannot create an individual program in the UI

This is the load-bearing problem; the rest is refinement on top of it.

- `Plan.objects.create` appears in exactly one place in app code —
  `MesoGroup.create_shared_plan` (`models.py:1488`), i.e. **groups only**. There
  is **no individual-plan create**, and **no add-mesocycle / add-week /
  add-session endpoint** in `meso/urls.py` (only `session_add_exercise` adds an
  exercise to an *existing* session). The designer is an **editor for
  pre-existing plan trees**, not a creator.
- Both individual-plan CTAs are dead ends:
  - **"+ New program"** (roster top nav, `roster.html:6`) → bare `/meso/designer/`
    → `_coach_working_plan` returns `None` for a fresh coach → redirect back to the
    roster with *"Pick an athlete to start a program."* (`views.py:156`).
  - **"Build a program"** (athlete profile empty state, `athlete_profile.html:131`)
    → same bare designer → same bounce.
- The only thing that builds an individual `Plan → Mesocycle → Week → Session →
  ExercisePrescription` tree is the `seed_meso_demo` management command
  (`seed_meso_demo.py:630`), which is admin-only.

**Net:** the group path (create group → design shared program → deliver) is wired
end-to-end, but a real (non-seeded) coach **cannot build an individual program at
all.** "Obvious to use" presupposes "usable," so this is Phase 1.

### Persona 1 — the cold / anonymous visitor

- `/meso/` is login-gated (`RosterView(LoginRequiredMixin)`), so an anonymous
  visitor is bounced straight to `/accounts/login/` with **no context**.
- There is **no landing or marketing page**, and Meso is **not linked from the
  main site** — nothing outside `app/store_project/meso/` references it; the only
  mount is `config/urls.py:38`. Discovery is effectively zero: you must already
  know the URL, and the first thing you meet is a bare login wall.

### Persona 2 — the brand-new coach

- Lands on `/meso/` and — post-#311 — is **redirected to `/meso/me/`** (the
  athlete home), because `RosterView.get` now treats only a user with a
  `CoachProfile` *or* a coach-side link *or* a sent invite as a coach
  (`views.py:201`). So a genuine new coach is shown the **athlete** surface and has
  **no path *onto* the coach surface**: there's **no self-serve `CoachProfile`
  creation** (only `seed_meso_demo.py:451` and the test factory create one), and
  the only "become a coach" action that exists — sending an invite — lives on the
  roster they can't reach (chicken-and-egg). This is the updated Persona 2 gap, and
  it sharpens the case for Q1 (allowlist + an explicit "become a coach" path).
- Once a coach *does* reach the roster, the empty states are passive — *"No
  athletes yet. Invite one to get started."* / *"No groups yet."* — and the actual
  actions (**"+ Invite an athlete"**, **"+ New group"**) are buried inside
  `<details>` disclosures (`roster.html:113`, `roster.html:154`).
- The one workflow that *should* be the core verb (build a program for an athlete)
  is the dead end described above.

### Persona 3 — the newly-invited athlete

- The best-supported journey. Invite email → claim link → `@login_required`
  bounces through allauth signup carrying `?next=` → returns to a clear confirm
  page (*"{coach} invited you to train"*, `invite_claim.html:30`) → **Accept** →
  lands on `/meso/me/` with a sensible empty state (*"No active programs yet. Once
  a coach delivers a week, it shows up here."*, `athlete_home.html:120`).
- Remaining gaps: the PWA is built but **install guidance is minimal** (only the
  gated notifications CTA); there's **no first-log nudge** on the session logger.
  The athlete-initiated path is now **mostly built** — #311 added a "Your coaches"
  card with incoming invites, sent requests, and a **request-a-coach** form
  (`athlete_home.html:41`) — so Phase 4 narrows to install + first-log polish.
- The athlete journey also silently depends on the coach being able to build +
  deliver — which, per the headline blocker, a fresh coach can't.

---

## Decisions this slice rests on

From [`decisions.md`](../../meso/decisions.md): **B1** (multi-coach SaaS), **B2** (athletes
are `User`s who log in; coach edits their plan), **N3** (roles: a user is a coach
via a `CoachProfile` *or* a coach-side link *or* a sent invite — post-#311
`RosterView` routes on exactly this, so the role is **load-bearing now, not
vestigial**; what's missing is a self-serve way to *acquire* it), **N4** (invites
built and now **bidirectional** — coach→athlete invite + athlete→coach request,
#311).

### Product decisions (resolved 2026-06-29)

These are genuine product calls; the owner delegated them. Resolved as below
(mirroring `decisions.md`'s ✅-Decided convention). **Q1 is the most consequential
— a real go-to-market stance — and the easiest to revisit.**

- **Q1 · Coach acquisition — self-serve signup, or invite/closed-beta only?**
  **✅ Decided: closed beta — coaches are allowlisted.** Not open self-serve.
  Forced by three facts: **no billing yet** (S6 deferred), each coach's agent runs
  call Claude Opus — a **real per-coach cost** — and it's a **single shared box**.
  Open signup is an unbounded cost/abuse vector. Mechanism: a lightweight
  allowlist (a setting/flag); `CoachProfile` **auto-creates on the first coach
  action for an allowlisted user** (so the owner + approved beta coaches are never
  blocked on admin surgery). Athletes stay self-serve via the existing invite
  claim. B1's multi-coach architecture is unchanged; flipping to open self-serve
  later is a one-flag retrofit, gated on the billing slice (S6).
- **Q2 · Individual plan creation shape — blank scaffold, template library, or
  agent-drafted-from-a-brief?**
  **✅ Decided & both shipped: blank scaffold first, agent-draft as a fast follow.**
  Phase 1 (PR #326) shipped the **blank-but-editable scaffold** (one mesocycle,
  one deliverable week, a couple of sessions) — the smallest change that kills the
  dead end. The **agent-draft fast follow** then shipped in **PR #335** (squash
  `f94d48c`, migration `0023`): a "Draft with AI" CTA hands the fresh scaffold to
  the agent to draft the first week into the existing review gate, on the back of
  a new **`add`** agent verb (introduce a new exercise row into a session — the
  others only edit existing rows). Metered like the manual agent run; degrades to
  a blank plan when the allowance is exhausted / no API key. See the decision-log
  entry. **Template library still deferred** — it needs a corpus of templates we
  don't have yet.
- **Q3 · One-click demo for a new coach?**
  **✅ Decided: yes.** A coach-scoped UI wrapper over the existing idempotent
  `seed_meso_demo` logic, with **tear-down** ("remove demo data"). Two guardrails:
  demo data is **clearly labeled** and **fully removable**, and demo athletes
  receive **no outbound email/push** (they aren't real people — the deliver
  notification hooks must skip them).
- **Q4 · Where does the landing page live + how visible is it?**
  **✅ Decided: a logged-out `/meso/` landing, request-access for coaches.** A
  public page explaining Meso with two honest entry actions — **"I have an invite"**
  (athlete) and **"Request coach access"** (beta, per Q1) — plus a single discreet
  link from the main site nav. **Not** an instant-signup funnel; the front door
  splits anon (landing) vs authenticated (role routing).

---

## Architecture / approach

The slice is mostly **templates + a few small endpoints/views**, in keeping with
the htmx/Alpine + small-JSON-seam style of the rest of Meso (B5). No new heavy
infrastructure.

**Reuse over new tables.** Same taste as the athlete + chat-thread slices: prefer
refactoring existing logic to adding models.

- **Plan creation (Phase 1).** Extract the seed's individual-plan tree-building
  (`seed_meso_demo.py:630` onward) into a reusable
  `Plan.create_individual(relationship, *, title, goal, unit, …)` + a
  `scaffold_plan(plan)` helper (one mesocycle, a current week, N empty sessions),
  so **both** the seed and the new endpoint share one code path. The decisions log
  already anticipated this need — `create_shared_plan` had to ship a starter
  scaffold "because there's no add-session/week endpoint yet" (decisions.md, groups
  Phase 2a). This slice pays that down with real **add-week / add-session**
  endpoints so a scaffold can grow.
- **Front door (Phase 3).** Split `/meso/` on auth: anonymous → a landing template
  (login-free, like `offline.html`); authenticated → the post-#311 role routing
  (non-coach → `/meso/me/`), plus a **"request coach access / become a coach"** path
  so a genuine new coach isn't stranded on the athlete surface. No new model.
- **Roles (Phase 2).** Self-serve `CoachProfile` via `get_or_create` on the first
  coach action (or an explicit "I'm a coach" choice from the front door), gated by
  the Q1 allowlist. The role already gates routing post-#311; this makes
  **acquiring** it self-serve.
- **Demo data (Phase 2).** A thin, coach-scoped wrapper over the existing
  `seed_meso_demo` building blocks (already idempotent, with `--delete`).
- **First-run guidance (Phases 4/5).** Empty-state CTAs, an install prompt, and
  dismissible coachmarks — template + small Alpine, with a per-user "seen" flag
  (session or a tiny profile boolean; **Q**: persist where).

New URLs (indicative, individual-side mirrors of the group-side ones):

```
GET  /meso/                         front door — landing (anon) | route (auth)
POST /meso/athlete/<uuid>/plan/new/ create an individual plan + scaffold → designer
POST /meso/api/plan/<id>/week/      add a week to a plan (designer)
POST /meso/api/plan/<id>/session/   add a session to a week (designer)
POST /meso/demo/load/               load demo athlete+program (coach-scoped)
POST /meso/demo/clear/              remove demo data
```

---

## Phasing (one PR each)

Phase 1 is load-bearing; **2–5 are refinement and can be reordered** once 1 lands.
Each phase is scoped to ship independently, red→green with tests, matching the
house discipline (scoping + access-control first).

### Phase 1 — Individual plan creation (the structural fix · coach)
Make the core verb real. `Plan.create_individual` + `scaffold_plan` (refactored
from the seed), a coach-scoped `POST /meso/athlete/<uuid>/plan/new/` that creates
the plan and lands in the designer, and **add-week / add-session** endpoints so the
scaffold is extensible. Wire the existing **"+ New program"** and **"Build a
program"** CTAs to actually create (and rename their dead-end copy).
*Done when:* a fresh coach can invite an athlete, click **New program**, and land
in a working, editable, deliverable designer — **with no seed required**.

### Phase 2 — Coach first-run & active empty states (coach) ✅ Built
Empty states that **teach the Invite → Build → Deliver model** (a first-run
onboarding card on an empty roster) and a **"Load a demo athlete & program"**
button (Q3, removable, no demo-athlete email/push) so a coach can explore a
populated app before committing real clients.
*Done when:* a brand-new coach immediately understands the 3-step model and can
either start for real or one-click a demo, then clear it.

> **Reconciled with `main` (Q1 dropped).** Q1's "allowlisted-coach access" was
> forced by *"no billing yet"*; **billing S6 shipped after this plan was written
> (Phases 1–5), and Phase 4 (#323) shipped open self-serve coach signup**
> (`become_coach` → `start_coaching` creates the `CoachProfile`). So the closed-beta
> allowlist is obsolete and was **not** built — it would contradict shipped
> behavior. Phase 2 therefore narrowed to the demo + empty-state teaching.
>
> **What shipped:** `meso/demo.py` (`load_demo` / `clear_demo` / `has_demo`) — a
> coach-scoped, idempotent wrapper over the `seed_meso_demo` data that stands up
> five demo athletes, a built/delivered/logged individual program, and a group
> (shared program + a couple of per-athlete overrides), all **namespaced per coach**
> (non-routable `@<coach-hex>.demo.invalid` addresses) so two coaches never collide.
> A new **`is_demo`** flag on `CoachAthlete` + `MesoGroup` (migration `0022`) makes
> demo data **clearly labeled** (roster banner + per-row "Demo" badge), **fully
> removable** (`clear_demo` deletes exactly the demo group + demo athlete users,
> cascading their links/plans/logs), and **billing-neutral** (a new
> `CoachAthlete.billable()` excludes demo links so `access.active_seat_count` /
> `suspended_athlete_ids` never let the demo trip the paywall). Demo athletes get
> **no email/push**: the load delivers weeks at the model layer (no notification),
> the addresses are non-routable, and each carries `delivery_email_opt_out`.
> `POST /meso/demo/load/` + `/meso/demo/clear/`; the meso base template now renders
> flashed messages (previously swallowed). +23 tests (`test_demo.py`).

### Phase 3 — The front door (anonymous visitor + routing) ✅ Built
A real logged-out `/meso/` landing (what Meso is · two entry actions — **"I have
an invite"** and **"Request coach access"**, per Q4/Q1), a single discreet **link
from the main site** so it's discoverable at all, and a **"become a coach / request
coach access"** path from the athlete home (where a brand-new user now lands
post-#311) so a genuine new coach isn't stranded on the athlete surface with no way
up. Note this is **distinct** from #311's *request-a-coach-to-train-under* form —
that's an athlete asking a coach to program for them; this is a user asking for the
coach role itself (Q1 beta access).
*Done when:* someone who's never heard of Meso lands on `/meso/`, understands it in
one screen, and is routed to the right surface (athlete home, or a path to coach
access).

> **What shipped (PR #329, no migration).** `RosterView` dropped
> `LoginRequiredMixin` and now **splits `/meso/` on auth**: an anonymous visitor
> renders the new login-free `meso/landing.html` (what Meso is + two honest entry
> actions — *log in as an athlete* with `?next=` back to the training home, and
> *become a coach* via the existing #323 funnel) instead of bouncing to
> `/accounts/login/`; an authenticated visitor keeps the post-#311 role routing
> (coach → roster, anyone else → `/meso/me/`) untouched. A discreet **"Coaching"**
> link in the main-site nav (`_nav.html`) makes Meso discoverable without already
> knowing the URL. **Q1's closed-beta "Request coach access"** was **not** built —
> obsoleted by #323's open self-serve signup (same reconciliation as Phase 2), so
> the coach entry action is the plain *become a coach* path; and the
> **become-a-coach-from-athlete-home** item already shipped in #323
> (`athlete_home.html`'s "Are you a coach?" card), so Phase 3 narrowed to the anon
> landing + the main-site link. +11 pytest (`test_landing.py`); the stale
> `test_roster_requires_login` became `test_anonymous_sees_landing_not_login`.
> Codex review CLEAN on iteration 1.

### Phase 4 — Athlete first-run polish (athlete) ✅ Built
An **install (PWA) prompt** and a one-time **first-log coachmark** on `/meso/me/`
and the session logger. The athlete-initiated join path that was the bulk of this
phase is **already built** (#311's "Your coaches" card + request-a-coach form), so
Phase 4 narrows to install + first-log polish.
*Done when:* a newly-invited athlete installs Meso and logs their first session
without confusion.

> **What shipped (PR #330, no migration).** The **first-log coachmark** is
> **server-driven** — `views._athlete_has_completed_log(user)` gates it on the home
> (only when a delivered session exists to tap) and the session logger, so it's
> naturally one-time + cross-device and vanishes the moment the first *completed*
> log lands (resolving the plan's open "persist where?" question with **no
> per-device flag and no migration**). It's gated on a `done` log specifically: a
> "Save progress" `pending` draft must not retract the hint that teaches the final
> "Log session" step. The **install prompt** is a new `meso_onboarding.js` that
> reveals the install card from a captured `beforeinstallprompt` (Chromium), falls
> back to manual *Add to Home Screen* steps on iOS (incl. iPadOS-13+ Safari's
> desktop "Macintosh" UA), stays hidden when already installed/dismissed, and
> persists manual coachmark dismissals; it's added to the SW precache (cache bumped
> v1→v2) so it works offline. Visibility toggles via inline `style.display` (an
> inline `display:flex` beats the UA `[hidden]` rule). +12 pytest
> (`test_athlete_onboarding.py` + a precache guard) + 13 vitest
> (`meso_onboarding.test.js`); Codex review loop **CLEAN** after 3 fix iterations.

### Phase 5 — Designer & agent self-explanation (coach · optional) ✅ Built
Dismissible first-run **coachmarks** on the designer's three regions (grid · agent
· phone preview), a one-line **"how the agent works"** note making the
propose → review → apply loop explicit (a first-timer won't expect the agent only
*proposes*), and replacing the static prototype left-rail chrome where it misleads.
*Done when:* a first-time coach in the designer understands the grid, the agent, and
the review gate without external help.

> **What shipped (no migration).** Three parts. **(1) Coachmarks** — three
> dismissible first-run notes anchor the designer's regions (week grid · agent ·
> phone preview). They show until dismissed; the dismissal persists client-side in
> `localStorage` (`meso.js`, keys namespaced `meso-coachmark-designer-<key>` so they
> never collide with the athlete onboarding coachmarks' `meso-coachmark-` prefix) —
> **no server "seen" flag, no migration**, like the athlete chrome (resolving the
> plan's "persist where?" Q for the coach side). **(2) Agent self-explanation** — a
> *persistent* "propose → review → apply" note under the agent header makes the
> review gate explicit for everyone, not just first-timers; individual-only (the
> group agent's composer is hidden). **(3) Real chrome** — `serialize_plan` now
> carries the individual plan's real athlete identity (`serialize_athlete_identity`:
> name / initials / goal / **active** contraindications — the same global injuries
> the agent grounds on), and the left-rail athlete card, macrocycle rail, top-bar
> identity/chip, and week/block headers render the real `athlete`/`weeks`/`phases`
> (new `meso.js` getters). The fabricated "Coach's programming style" block was
> removed as misleading; group mode (already real, via `group`) is untouched. +10
> pytest (`test_designer_onboarding.py`) + 5 vitest (`meso.test.js`); Codex review
> loop CLEAN on iteration 1. **This completes the first-time-UX slice (Phases 1–5).**

---

## Out of scope (later)

Native apps · a full marketing site · billing/subscriptions for Meso (B1 / S6) · an
athlete↔coach discovery marketplace · a from-scratch designer redesign · the full
"changes since last delivery" diff UI (tracked elsewhere).

---

## Testing

pytest + factory_boy, mirroring the coach/athlete-side discipline. Priorities:

- **Plan creation (Phase 1):** the create endpoint is **coach-scoped** (a foreign
  or unknown athlete → 404/403); a created plan is rooted at the right
  `CoachAthlete` relationship and is immediately **editable + deliverable**; the
  full **create → edit → deliver → athlete-sees-it** round trip works **without the
  seed**; `create_individual`/`scaffold_plan` and the seed share one path
  (refactor regression-guarded).
- **Routing (Phase 3):** anonymous `/meso/` → landing (not a bare login bounce);
  pure athlete → `/me/`; coach → roster; the explicit role fork resolves the
  no-role state.
- **Roles & demo (Phase 2):** first coach action creates a `CoachProfile`
  (idempotent); demo load/clear is idempotent and **coach-scoped** (never touches
  another coach's data).
- **Empty states / first-run:** correct CTAs render in the empty account; a
  dismissed coachmark stays dismissed.

As in prior slices there's no in-project JS test runner for some template/Alpine
bits — guard those at the source/render level (the `test_designer_agent_chat.py`
precedent), and cover the rest with Vitest where helpers are pure.

---

## Decisions (resolved 2026-06-29)

| # | Question | ✅ Decision |
|---|----------|------------|
| Q1 | Coach acquisition — self-serve or invite-only? | **Closed beta** — allowlisted coaches; `CoachProfile` auto-creates on first coach action for an allowlisted user; open self-serve deferred to billing (S6). *(Most consequential — easiest to revisit.)* |
| Q2 | Plan-creation shape — blank / template / agent-drafted? | **Blank scaffold first** (Phase 1), **optional agent draft** as a fast follow (reuses the review gate); template library deferred |
| Q3 | One-click demo for new coaches? | **Yes** — coach-scoped wrapper over `seed_meso_demo`, removable, no demo-athlete email/push |
| Q4 | Landing page location + visibility? | **Logged-out `/meso/` landing** with "I have an invite" + "Request coach access" + one main-site link; not instant-signup |
