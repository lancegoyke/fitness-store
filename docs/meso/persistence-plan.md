# Meso — persistence slice plan

**Status:** in progress — Phase 1 shipped & deployed 2026-06-27 (PR #270); Phase 2 merged
2026-06-27 (PR #271); Phase 3 merged 2026-06-27 (PR #274); Phase 4 merged 2026-06-27
(PR #276); Phase 5 next · created 2026-06-26
**Companion to:** [`decisions.md`](./decisions.md)
**Goal of this slice:** turn the **coach-side** screens (designer, roster, athlete profile)
from client-side mocks into real, DB-backed, **tenant-scoped** data. No agent, no athlete app
yet — the review and results screens keep running on seeded data until their own slices.

This slice is bigger than plain CRUD: because Meso is multi-coach (B1) with a many-to-many,
athlete-consented relationship (N1), it has to carry the tenancy + roles + relationship spine.

### Decisions this rests on (see `decisions.md`)
- **B1** multi-coach SaaS · **B2** athletes are Users who log in, coaches edit their plans ·
  **B4** hybrid exercise source · **N1** many-to-many, athlete-consented.
- **D-a** plans owned per coach↔athlete relationship · **D-b** contraindications global to the
  athlete, goals per-plan · **D-c** bidirectional invites, either side can end.

---

## Architecture

Models live in the **`meso`** app (`store_project/meso/models.py`) to keep the feature cohesive;
they reference `settings.AUTH_USER_MODEL` (the existing UUID `User`) rather than subclassing it.
No new third-party deps: htmx + Alpine (already bundled), allauth for accounts, `django-ses` +
the `notifications` app for invite email. Front-end stays server-rendered (htmx) except the
designer grid, which gets a small JSON autosave endpoint (plain `JsonResponse`, no DRF).

### Entity sketch

```
User ──1:1── CoachProfile
User ──1:1── AthleteProfile ──< Contraindication        (global to the athlete, D-b)

CoachAthlete (coach=User, athlete=User, status, invited_by)   ← the M2M through table (N1)
   └──< Plan (goal, status, unit)                              (owned per relationship, D-a)
          └──< Mesocycle (phase, order, week_count)
                 └──< Week (volume, intensity, is_deload, is_current, delivered_at)
                        └──< Session (day_number, name, bias)
                               └──< ExercisePrescription
                                      (exercise→exercises.Exercise NULLABLE, name, sets,
                                       reps, load, rpe, note, tags)            ← hybrid (B4)

Session ──< SessionLog (athlete, date, status) ──< LoggedSet (prescription, reps, load, rpe)
Week ──< WeekDelivery (delivered_at, payload JSON)            ← snapshot for "changes since…"
```

---

## Data model (field-level)

**Tenancy / roles / relationship**
- `CoachProfile(user 1:1, display_name, programming_style JSON[list], avoid_rules text, default_unit[kg|lb])` — presence = is-a-coach.
- `CoachAthlete(coach→User, athlete→User, status, invited_by, created_at, responded_at, ended_at, token UUID)`
  - `status ∈ {pending_coach_invite, pending_athlete_request, active, declined, ended}`
  - `invited_by ∈ {coach, athlete}`; `unique(coach, athlete)`; check `coach_id != athlete_id`.
  - `token` backs the signed accept/decline URL.
- `AthleteProfile(user 1:1, training_started date, notes)` — global athlete attrs (training age = derived).
- `Contraindication(athlete→User, text, active, created_at)` — **global** to the athlete (D-b).

**Program (per relationship — D-a)**
- `Plan(relationship→CoachAthlete, title, goal, status[draft|active|archived], unit[kg|lb], created/updated)`; `coach`/`athlete` are properties off `relationship`.
- `Mesocycle(plan, name, order, week_count)`.
- `Week(mesocycle, index, phase, volume:int, intensity:int, is_deload, is_current, delivered_at:null)`.
- `Session(week, day_number, name, bias, order)`.
- `ExercisePrescription(session, exercise→exercises.Exercise null/blank, name, order, sets, reps, load, rpe, note, tags JSON[list])`
  - `sets/reps/load/rpe` are **CharField** — the prototype grid is free-form (`load="BW"`, `rpe="—"`, rep ranges). Numeric coercion happens at read time, as the JS already does.
  - `exercise` nullable = the hybrid (B4): linked → picker/alternatives/contraindication-matching; null → free text in `name`.

**Logging (models now, UI in the athlete slice)**
- `SessionLog(session, athlete→User, date, status[pending|done], notes)`.
- `LoggedSet(session_log, prescription→ExercisePrescription null, set_number, reps, load, rpe)`.

**Delivery / lightweight versioning**
- `WeekDelivery(week, delivered_at, payload JSON)` — serialized snapshot of the week at delivery.
  "Changes since last delivery" = diff(current serialization, latest `payload`). Full diff **UI**
  is deferred to the agent/review slice; this just captures the data cheaply.

---

## Scoping & permissions (N2)

- Scoped managers: `Plan.objects.for_coach(user)` → plans whose `relationship.coach == user` and
  `relationship.status == active`; `Plan.objects.for_athlete(user)` → plans across all the
  athlete's active coaches.
- A coach's **roster** = athletes with an `active` `CoachAthlete` to them.
- Edit a plan ⇒ `user == plan.relationship.coach` and active. Athletes get **read + logging only**.
- View mixin `CoachOwnsPlanMixin` / `AthleteOwnsSessionMixin` enforce object-level access; every
  list view goes through a scoped manager, never an unscoped `.all()`.

## Invites / onboarding (N4, bidirectional — D-c)

`CoachAthlete.status` state machine:

```
coach invites  → pending_coach_invite ─(athlete accepts)→ active ─(either ends)→ ended
athlete asks   → pending_athlete_request ─(coach accepts)→ active
any pending    ─(recipient declines)→ declined        (re-invite reopens a fresh row/status)
```

- Coach invites by email: find-or-stub a `User`, create `pending_coach_invite`, email a tokened
  accept link (`notifications` + `django-ses`). New users complete signup via allauth, then accept.
- Athlete requests a coach via a coach's share link → `pending_athlete_request`.
- Ending a relationship sets `ended` and **archives** that coach's plans (never deletes); the
  other coaches' plans are untouched.

## Front-end (B5)

- **Roster / profile / relationship management:** htmx, server-rendered from scoped querysets.
- **Designer:** `meso.js` stops owning fixtures. The view serializes the plan to a JSON blob in
  the page; `meso.js` **hydrates** from it, then **autosaves** edits to ownership-checked endpoints:
  - `POST /meso/api/plan/<id>/prescription/<pid>/` — patch a cell (or a small batch).
  - `POST /meso/api/plan/<id>/session/<sid>/exercise/` — add an exercise.
  - `POST /meso/api/plan/<id>/deliver/` — stamp `delivered_at` + write a `WeekDelivery`.
  - All return JSON; all check `request.user == plan.relationship.coach`.

---

## Phasing (one PR each)

**Phase 1 — Roles + relationships. ✅ Done (2026-06-26).**
Models: `CoachProfile`, `AthleteProfile`, `CoachAthlete`, `Contraindication`. Migrations, admin,
scoped managers, the invite/accept state machine (tokened URLs; email send deferred), and roster +
athlete-profile reading **real** scoped data (replacing those mocks). Factories + tests for scoping
and the invite flow.
*Done when:* a coach sees only their athletes; an athlete can accept/decline/end; roster + profile
render from the DB.

*Shipped:* the four models live in `store_project/meso/models.py` (`CoachAthlete` carries the
state machine + `CoachAthleteQuerySet`); tokened `accept`/`decline`/`end` POST views; presenters
(`presenters.py`) map real models → the existing template shape, with Phase-2/3 fields
(compliance, block/week, status, macrocycle, results) shown as honest placeholders until their
slices. Athlete profile now routes by `User` UUID (`/meso/athlete/<uuid:pk>/`), not a mock slug.
28 meso tests (scoping + invite machine + screen-render smoke). **Deferred to a follow-up:** the
actual invite *email* send (django-ses + `notifications`) — only the state machine + tokened URLs
landed here.

**Phase 2 — Program schema. ✅ Done (2026-06-27).**
Models: `Plan → Mesocycle → Week → Session → ExercisePrescription` + the hybrid `Exercise` FK.
Admin, a plan→JSON serializer, factories. *Done when:* a seeded plan round-trips to the designer's
expected JSON shape.

*Shipped* (branch `meso-persistence-phase2`): the program hierarchy lives in
`store_project/meso/models.py` (`Plan` with a `PlanQuerySet` — `for_coach`/`for_athlete`/`active`
— and `coach`/`athlete` properties off `relationship`; `Mesocycle`/`Week`/`Session` with their
ordering + `unique` constraints; `ExercisePrescription` with the nullable `exercise` FK = the B4
hybrid + free-form Char `sets`/`reps`/`load`/`rpe`). Logging models `SessionLog`/`LoggedSet` are
defined now (UI later). `CoachAthlete.end()` now archives the relationship's plans (D-c). The
plan→JSON serializer is `meso/serializers.py` (`serialize_plan` → the designer's
`plan`/`program`/`weeks`/`phases` shape; `tags[]`→`tag`; `last`/`adj` deferred to the log/agent
slices). Migration `meso.0002`; admin with nested inlines; factories for all seven models. Built
test-first (red→green): 18 new tests (`test_program_models.py` + `test_serializers.py`, the Maya
round-trip) on top of Phase 1's 28 — 46 meso tests, 186 project-wide, green. **Merged as PR #271.**

**Phase 3 — Designer save/load. ✅ Done (2026-06-27).**
Hydrate `meso.js` from the serialized plan; the JSON autosave endpoints above; ownership checks.
*Done when:* editing a cell / adding an exercise persists and survives reload.

*Shipped* (branch `meso-persistence-phase3`, **PR #274**): `MesoDesignerView` now serves
`/meso/designer/<plan_id>/`, serializing an owned `Plan` (via Phase 2's `serialize_plan`) into the
page through `{{ plan_data|json_script }}`; `meso.js` `init()` hydrates `program`/`weeks`/`phases`/
`unit`/`planId` from it, falling back to the prototype fixtures when no plan is injected (retired in
Phase 5). Two plain-`JsonResponse` endpoints (no DRF), each scoped via `_coach_plan_or_forbidden`
(non-owner / inactive relationship → **403**; child not under the plan → **404**):
`POST api/plan/<id>/prescription/<pid>/` patches grid cells (type + `max_length` validated → 400)
and `POST api/plan/<id>/session/<sid>/exercise/` appends a blank row (order = max + 1). Cell edits
autosave on `@change`; "+ Add exercise" adopts the server id. Built red→green: 19 new tests
(`test_designer_save.py`) covering load/serialize, ownership, child-scoping, validation, method/login
guards, and patch-survives-reload — 66 meso / 206 project-wide pass. Local Codex review: clean.
**Deferred:** CSRF is wired (token injected, `X-CSRFToken` sent) but a save-failure UI (the static
"Autosaved" indicator stays put on error) is left for a follow-up.

**Phase 4 — Deliver (lightweight). ✅ Done (2026-06-27).**
`delivered_at` + `WeekDelivery` snapshot; wire the deliver screen to a real action.
*Done when:* delivering stamps the week and records a snapshot.

*Shipped* (branch `meso-persistence-phase4`, **PR #276**): `WeekDelivery(week, delivered_at,
payload JSON)` + migration `meso.0003` (admin: Week inline + standalone read-only; factory).
`POST api/plan/<id>/deliver/` stamps the plan's current week (`serializers.current_week`, promoted
from the former `_current_week`) and writes a `serialize_week_snapshot` of its sessions/
prescriptions, behind the same `_coach_plan_or_forbidden` ownership check (non-owner / inactive →
403; plan with no week → 400). The deliver screen gains a real plan-bound mode
`/meso/deliver/<plan_id>/` (the no-arg URL stays on fixtures until Phase 5, mirroring the designer);
its button POSTs the real action and the designer's Deliver link targets it when a plan is loaded.
Built red→green: 14 new tests (`test_deliver.py`) — 80 meso / 220 project-wide pass. Local Codex
review: clean. **Deferred:** scheduling, push/email notifications, and the full "changes since last
delivery" diff UI (the snapshot is captured now; the diff renders with the agent/athlete slices).

**Phase 5 — Seed + retire mock.**
`seed_meso_demo` management command (demo coach = you, the demo athletes, relationships, a sample
plan); remove `mockdata.py` for coach-side screens. *Done when:* a fresh dev DB shows the same
screens, now real. (Review/results stay seeded until their slices.)

## Out of scope (later slices)
Real agent + `ProposedChange` (agent slice) · athlete logging UI + PWA + notifications (athlete
slice) · groups = shared program + per-athlete override (after individuals).

## Testing
pytest + factory_boy (already in use). Priorities: **scoping** (coach A cannot read/edit coach B's
plans; athlete sees all their coaches'), the **invite state machine**, and **autosave ownership**
(non-owner POST → 403).

## Open assumptions (carried from the plan; flag to override)
1. ~~**D-a/D-b/D-c** as recorded in `decisions.md`.~~ — **locked** (decisions log, 2026-06-26).
2. ~~Roles live in the **`meso`** app (not `users`).~~ — **confirmed**; built there in Phase 1.
3. ~~"Changes since last delivery" = **snapshot-per-delivery now**, full diff UI deferred.~~ —
   **snapshot done** in Phase 4 (`WeekDelivery.payload`); the full diff *UI* is still deferred to
   the agent/athlete slice.
4. ~~**Logging models defined now**, UI later.~~ — **done**: `SessionLog`/`LoggedSet` built in
   Phase 2 (models + admin + factories only; athlete-facing logging UI lands with the athlete slice).
