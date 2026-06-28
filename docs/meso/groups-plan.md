# Meso — groups slice (S1) plan

**Status:** living document · started 2026-06-28
**Decision context:** [`decisions.md`](./decisions.md) S1 — *Groups: "shared program +
per-athlete auto-adjust" modeling (template + override diffs)*. Deferred until individuals
worked; the individual coach + athlete + agent slices are now complete, so groups is the
main remaining Meso feature area.

## What a group is

A coach often programs **one shared template** for several athletes who train together (a
team, a class, a squad), then lets each athlete's plan **auto-adjust** off that shared
base — a per-athlete load %, an exercise swap for a contraindication, a volume tweak. The
prototype already carries this shape:

- the **roster** has a *Groups* card (`groups`, member avatars, `focus`, a status badge) —
  wired to a dead `[]` today (`RosterView.get_context_data`);
- the **designer** has an Individual / **Group** mode toggle, a "Flags across group" panel,
  and a "**Shared program · per-athlete auto-adjusts**" row with a per-row `adj` badge
  (`ex.adj` in `meso.js`).

So the slice is: stand up real group rows, render them, then make the shared-program +
per-athlete-override engine real behind that UI.

## Modeling decision — template + override diffs (S1)

A group's shared program **reuses the existing `Plan → Mesocycle → Week → Session →
ExercisePrescription` tree** rather than a parallel schema. A *group plan* is a `Plan`
rooted at a group (not at one coach↔athlete relationship), and each member's effective
program = **shared template + that member's override diffs** (the `adj` overlay). This keeps
the designer, serializers, agent validation, and delivery working on one program shape;
groups add a root (`group`) and an overlay (`PrescriptionOverride`), not a second hierarchy.

Members are the coach's **active** athletes: a `GroupMembership` references the
`CoachAthlete` link, so membership structurally guarantees an active coaching relationship
and per-athlete overrides/delivered plans hang off the same relationship that owns
individual plans (D-a). Ending a link hides the member (read-side scopes to active links);
the membership row persists so reopening the link restores them — no change to
`CoachAthlete.end()`.

## Phasing

Small vertical slices, red→green, each its own PR — the same cadence as the persistence,
agent, and athlete slices.

- **Phase 1 — group foundation + roster/detail read surface (this PR).** `MesoGroup`
  (coach-owned: name, focus, status) + `GroupMembership` (group ↔ active `CoachAthlete`).
  Scoped queryset (`for_coach` / `active`), add/remove-member helpers (guard same-coach +
  active link). The roster *Groups* card lights up from real rows, and a new
  `GroupDetailView` (`/meso/group/<id>/`) lists members + their cross-group contraindication
  flags (the prototype's "Flags across group"). Admin + factories + a seeded demo group.
  **No shared program yet** — the roster/detail read the membership, not a plan.
- **Phase 2 — shared group program.** `Plan.group` FK (+ nullable `relationship`); a group
  plan is a `Plan` rooted at a group. The designer renders a group plan in Group mode;
  scoping (`Plan.objects.for_group` / coach can edit their group's plan). Create-group UI.
- **Phase 3 — per-athlete overrides (the `adj` overlay).** `PrescriptionOverride` per member
  (load %, swap, volume), effective-program resolution (`shared + overrides`), the designer's
  "Shared program · per-athlete auto-adjusts" row + per-row `adj` badge driven by real diffs.
- **Phase 4 — deliver to all members.** Delivering a group week fans out a per-athlete
  delivered snapshot (each member's *resolved* program), reusing the athlete surface + the
  delivery email/push from the athlete slice.

## Phase 1 — build notes

- **Models** (`meso/models.py`): `MesoGroup(coach FK, name, focus, status, created,
  modified)` with a `Status` (DRAFT/ACTIVE/ARCHIVED, default ACTIVE) and a
  `MesoGroupQuerySet` (`for_coach`, `active`); `GroupMembership(group FK, relationship FK →
  CoachAthlete, created_at)` with `unique(group, relationship)`. Helpers on `MesoGroup`:
  `add_athlete(athlete)` (looks up the coach's *active* link, raises if absent or
  cross-coach, idempotent), `remove_athlete(athlete)`, and `active_member_users()` (members
  whose link is still active). Migration `meso.0007`.
- **Scoping:** a coach sees only their own groups; a group's displayed members are scoped to
  *active* links so an ended relationship drops out without deleting the membership row.
- **Read surface:** `presenters.roster_group(group)` (name, focus, member avatars, a `meta`
  line, status label) feeds the roster card; `presenters.group_detail(group)` feeds
  `GroupDetailView` (members + each member's active contraindication labels, folded into a
  unique "flags across group" set). The roster card links to the detail page (was a dead
  link to the designer).
- **Seed:** `seed_meso_demo` stands up one demo group ("Tue/Thu Strength Squad") with three
  of the demo athletes as members, so a fresh DB renders the *Groups* card.
- **No agent / no shared program / no override engine** in Phase 1 — those are Phases 2–3.
  The point of Phase 1 is the tenancy-correct group + membership spine and the read surface,
  exactly as persistence Phase 1 shipped the relationship spine before the program schema.

## Tests (Phase 1)

`meso/tests/test_groups.py` — model + scoping + read surface:

- `MesoGroup.for_coach` isolates per coach; `active()` excludes archived.
- `add_athlete` creates a membership off the active link; raises on no-link / cross-coach /
  self; is idempotent; `remove_athlete` drops it.
- `active_member_users()` excludes members whose link ended (without deleting the row), and
  restores them when the link is reopened.
- `roster_group` / `group_detail` shapes (avatars, meta, folded flags).
- `RosterView` renders the coach's groups and not another coach's; `GroupDetailView` is
  coach-scoped (foreign/unknown group → 404) and lists members + flags.
