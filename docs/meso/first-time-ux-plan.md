# Meso — first-time UX / onboarding slice plan

**Status:** 🟡 Proposed · decisions Q1–Q4 resolved 2026-06-29 · build not started
**Companion to:** [`decisions.md`](./decisions.md) (B1 multi-coach, B2 athlete
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

### 🚧 Headline blocker — a coach cannot create an individual program in the UI

This is the load-bearing problem; the rest is refinement on top of it.

- `Plan.objects.create` appears in exactly one place in app code —
  `MesoGroup.create_shared_plan` (`models.py:1289`), i.e. **groups only**. There
  is **no individual-plan create**, and **no add-mesocycle / add-week /
  add-session endpoint** in `meso/urls.py` (only `session_add_exercise` adds an
  exercise to an *existing* session). The designer is an **editor for
  pre-existing plan trees**, not a creator.
- Both individual-plan CTAs are dead ends:
  - **"+ New program"** (roster top nav, `roster.html:6`) → bare `/meso/designer/`
    → `_coach_working_plan` returns `None` for a fresh coach → redirect back to the
    roster with *"Pick an athlete to start a program."* (`views.py:147`).
  - **"Build a program"** (athlete profile empty state, `athlete_profile.html:131`)
    → same bare designer → same bounce.
- The only thing that builds an individual `Plan → Mesocycle → Week → Session →
  ExercisePrescription` tree is the `seed_meso_demo` management command
  (`seed_meso_demo.py:598`), which is admin-only.

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

- Lands on `/meso/` and — because they have **no `CoachProfile`** *and* no athlete
  link — falls through to the **empty coach roster** (`RosterView.get`,
  `views.py:181`). Note the implicit role assumption: the redirect to `/me/` only
  fires when `not is_coach and is_athlete` (`views.py:184`), so **any logged-in
  non-athlete is silently treated as a coach.** There is no explicit "coach or
  athlete?" fork and **no self-serve `CoachProfile` creation** (only
  `seed_meso_demo.py:442` and the test factory create one).
- The empty states are passive — *"No athletes yet. Invite one to get started."*
  / *"No groups yet."* — and the actual actions (**"+ Invite an athlete"**,
  **"+ New group"**) are buried inside `<details>` disclosures (`roster.html:79`,
  `roster.html:120`).
- The one workflow that *should* be the core verb (build a program for an athlete)
  is the dead end described above.

### Persona 3 — the newly-invited athlete

- The best-supported journey. Invite email → claim link → `@login_required`
  bounces through allauth signup carrying `?next=` → returns to a clear confirm
  page (*"{coach} invited you to train"*, `invite_claim.html:28`) → **Accept** →
  lands on `/meso/me/` with a sensible empty state (*"No active programs yet. Once
  a coach delivers a week, it shows up here."*, `athlete_home.html:69`).
- Gaps: the PWA is built but **install guidance is minimal** (only the gated
  notifications CTA, `athlete_home.html:25`); there's **no first-log nudge** on the
  session logger; and there's **no athlete-initiated path** ("I have a coach — ask
  them to invite me / enter a code"), an N4 follow-up.
- The athlete journey also silently depends on the coach being able to build +
  deliver — which, per the headline blocker, a fresh coach can't.

---

## Decisions this slice rests on

From [`decisions.md`](./decisions.md): **B1** (multi-coach SaaS), **B2** (athletes
are `User`s who log in; coach edits their plan), **N3** (roles: `CoachProfile`
presence = is-a-coach — today effectively *vestigial for access*, since no view
gates on it), **N4** (email invites built — the athlete's join path exists).

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
  **✅ Decided: blank scaffold first, agent-draft as a fast follow.** Phase 1 ships
  a **blank-but-editable scaffold** (one mesocycle, one deliverable week, a couple
  of empty sessions) — the smallest change that kills the dead end. Then layer an
  **optional agent draft** ("draft a starting block from this goal") reusing the
  existing proposal engine behind the review gate. **Template library deferred** —
  it needs a corpus of templates we don't have yet.
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
  (`seed_meso_demo.py:598` onward) into a reusable
  `Plan.create_individual(relationship, *, title, goal, unit, …)` + a
  `scaffold_plan(plan)` helper (one mesocycle, a current week, N empty sessions),
  so **both** the seed and the new endpoint share one code path. The decisions log
  already anticipated this need — `create_shared_plan` had to ship a starter
  scaffold "because there's no add-session/week endpoint yet" (decisions.md, groups
  Phase 2a). This slice pays that down with real **add-week / add-session**
  endpoints so a scaffold can grow.
- **Front door (Phase 3).** Split `/meso/` on auth: anonymous → a landing template
  (login-free, like `offline.html`); authenticated → today's role routing, made
  explicit. No new model.
- **Roles (Phase 2).** Self-serve `CoachProfile` via `get_or_create` on the first
  coach action (or an explicit "I'm a coach" choice from the front door). The
  `CoachProfile` stops being vestigial.
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

### Phase 2 — Coach first-run & active empty states (coach)
Allowlisted-coach access (Q1): `CoachProfile` **auto-creates on the first coach
action for an allowlisted user** (non-allowlisted users see "request access", not
the roster). Empty states that **teach the Invite → Build → Deliver model** with
inline CTAs promoted out of the `<details>` disclosures, and a **"Load a demo
athlete & program"** button (Q3, removable, no demo-athlete email/push) so a coach
can explore a populated app before committing real clients.
*Done when:* an allowlisted brand-new coach immediately understands the 3-step
model and can either start for real or one-click a demo, then clear it.

### Phase 3 — The front door (anonymous visitor + routing)
A real logged-out `/meso/` landing (what Meso is · two entry actions — **"I have
an invite"** and **"Request coach access"**, per Q4/Q1), a single discreet **link
from the main site** so it's discoverable at all, and an **explicit role fork** for
the ambiguous "logged in, no role yet" state (replacing the silent
non-athlete-=-coach assumption; a non-allowlisted user lands on request-access, not
a coach roster).
*Done when:* someone who's never heard of Meso lands on `/meso/`, understands it in
one screen, and is routed to the right surface (or to request-access).

### Phase 4 — Athlete first-run polish (athlete)
An **install (PWA) prompt** and a one-time **first-log coachmark** on `/meso/me/`
and the session logger, plus (optionally) an **athlete-initiated** affordance
("Have a coach? Ask them to invite you / enter an invite code") — an N4 follow-up
that closes the loop for an athlete who arrives first.
*Done when:* a newly-invited athlete installs Meso and logs their first session
without confusion.

### Phase 5 — Designer & agent self-explanation (coach · optional)
Dismissible first-run **coachmarks** on the designer's three regions (grid · agent
· phone preview), a one-line **"how the agent works"** note making the
propose → review → apply loop explicit (a first-timer won't expect the agent only
*proposes*), and replacing the static prototype left-rail chrome where it misleads.
*Done when:* a first-time coach in the designer understands the grid, the agent, and
the review gate without external help.

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
