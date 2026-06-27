# Meso — persistence slice plan

**Status:** proposed · 2026-06-26
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
scoped managers, the invite/accept state machine + email, and roster + athlete-profile reading
**real** scoped data (replacing those mocks). Factories + tests for scoping and the invite flow.
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

**Phase 2 — Program schema.**
Models: `Plan → Mesocycle → Week → Session → ExercisePrescription` + the hybrid `Exercise` FK.
Admin, a plan→JSON serializer, factories. *Done when:* a seeded plan round-trips to the designer's
expected JSON shape.

**Phase 3 — Designer save/load.**
Hydrate `meso.js` from the serialized plan; the JSON autosave endpoints above; ownership checks.
*Done when:* editing a cell / adding an exercise persists and survives reload.

**Phase 4 — Deliver (lightweight).**
`delivered_at` + `WeekDelivery` snapshot; wire the deliver screen to a real action.
*Done when:* delivering stamps the week and records a snapshot.

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
1. **D-a/D-b/D-c** as recorded in `decisions.md`.
2. Roles live in the **`meso`** app (not `users`).
3. "Changes since last delivery" = **snapshot-per-delivery now**, full diff UI deferred.
4. **Logging models defined now**, UI later.
