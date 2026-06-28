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

- **Phase 1 — group foundation + roster/detail read surface (built; this PR).** `MesoGroup`
  (coach-owned: name, focus, status) + `GroupMembership` (group ↔ active `CoachAthlete`).
  Scoped queryset (`for_coach` / `active`), add/remove-member helpers (guard same-coach +
  active link). The roster *Groups* card lights up from real rows, and a new
  `GroupDetailView` (`/meso/group/<id>/`) lists members + their cross-group contraindication
  flags (the prototype's "Flags across group"). Admin + factories + a seeded demo group.
  **No shared program yet** — the roster/detail read the membership, not a plan.
- **Phase 2 — shared group program.** `Plan.group` FK (+ nullable `relationship`); a group
  plan is a `Plan` rooted at a group. The designer renders a group plan in Group mode;
  scoping (`Plan.objects.editable_by` / coach can edit their group's plan). Create-group UI.
  Split — like the athlete slice's Phase 4 — into:
  - **Phase 2a — shared program spine + Group-mode designer (built; this PR).** `Plan.group`
    FK + nullable `relationship` + a DB `XOR` constraint (a plan is rooted at *exactly one* of
    a relationship or a group). `Plan.coach/athlete/is_group/is_editable_by`,
    `PlanQuerySet.editable_by` (the designer + autosave surface — individual *or* group;
    `for_coach` stays individual-only so the deliver/results/review flows never see a group
    plan), `MesoGroup.shared_plan()/create_shared_plan()` (a starter scaffold so the grid is
    immediately editable). The designer opens a group plan and renders Group mode off real
    rows (`serialize_group_identity`); autosave edits its grid; **deliver + the agent reject
    group plans** (Phases 4/3). A `group_design` POST entry point + the group-detail
    "Design / Open shared program" card. The seeded demo group gets a shared program.
  - **Phase 2b — create-group UI.** Create a brand-new group from the roster (name + focus +
    pick members), so groups no longer come only from the seed/admin.
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
- **Membership tenancy (defense in depth, from the Codex review):** besides
  `add_athlete`'s guard, `active_member_users` filters `relationship.coach == group.coach` (a
  row written outside the helper — e.g. a raw admin inline — can never leak a foreign coach's
  athlete onto the read surface), and `GroupMembership.clean` rejects a cross-coach
  relationship and an inactive link **on creation only** (`self._state.adding`), so a
  membership row whose link *later* ended stays re-savable in the admin.
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

## Phase 2a — build notes

- **A plan is rooted at exactly one of relationship / group.** `Plan.relationship` is now
  nullable and `Plan.group` (FK → `MesoGroup`, `related_name="plans"`) is added, guarded by a
  `plan_relationship_xor_group` `CheckConstraint` — both-set or neither-set is a DB error, so
  the program tree (`Mesocycle → … → ExercisePrescription`) is reused for both kinds, gaining
  only a root (and, Phase 3, an override overlay). Migration `meso.0008`.
- **Accessors:** `Plan.coach` resolves via the relationship *or* the group; `Plan.athlete` is
  `None` for a group plan (so `__str__` names the group); `Plan.is_group` /
  `Plan.is_editable_by(user)` drive the gates below.
- **Scoping — the key tenancy call:** `PlanQuerySet.editable_by(user)` is the wider gate the
  *designer + autosave* use — an individual plan over an active relationship **or** a group
  plan the coach owns. `for_coach` deliberately stays **individual-only**: it backs the
  deliver / results / review flows, which assume a single athlete, so a group plan must never
  leak into them. `for_athlete` is unchanged (athletes reach a group's program through the
  per-athlete delivered snapshot in Phase 4, not the group plan).
- **Shared-plan helpers:** `MesoGroup.shared_plan()` is the group's current non-archived plan
  (or `None`); `create_shared_plan()` builds one rooted at the group with a minimal starter
  scaffold (one block, the current week, two days each with a starter row) — there is no
  add-session/add-week endpoint yet, so a bare plan would be uneditable.
- **Designer:** `MesoDesignerView` opens a group plan via `editable_by`; `serialize_plan`
  carries a `group` identity payload (`serialize_group_identity`: name/focus, active members +
  avatars + each one's flags, and the folded "flags across group") and **skips** the
  athlete-scoped "last time" column for a group plan. `meso.js` flips to Group mode and
  hydrates the top-bar + left-rail identity from that payload (no more hardcoded squad); the
  fabricated per-athlete adjusts are replaced with an honest Phase-3 placeholder.
  `initials` moved to `serializers` so the payload builds without a presenters import cycle.
- **What a group plan can't do yet:** deliver (`plan_deliver`) and the agent (`agent_propose`)
  reject a group plan with a `400` — deliver-to-all is Phase 4, the group agent is Phase 3, and
  both dereference `plan.athlete`. The designer hides Review / Deliver and the agent composer in
  Group mode to match. Autosave (`prescription_patch` / `session_add_exercise`) is
  athlete-agnostic and works on a group plan.
- **Entry point:** `group_design` (POST `/meso/group/<id>/design/`) get-or-creates the group's
  shared plan and redirects into the designer; the group-detail page offers "Design shared
  program" (none yet) or "Open shared program" (one exists). Idempotent — a double-submit never
  spawns a second plan. The seed gives the demo group a shared program (created once).

## Tests (Phase 2a)

`meso/tests/test_group_program.py` — model + scoping + serializer + the designer/endpoints:

- The `XOR` root constraint (both-set and neither-set rejected); `coach`/`athlete`/`is_group`/
  `__str__` for a group plan.
- `editable_by` includes an owned group plan + an owned active individual plan, excludes a
  foreign group plan and an ended individual plan; `for_coach` stays individual-only;
  `for_athlete` excludes group plans; `is_editable_by`.
- `shared_plan()` (None / created / excludes archived); `create_shared_plan()` roots at the
  group with a scaffold.
- `serialize_plan` carries the `group` payload (members + folded flags), omits it for an
  individual plan, and skips the `last` column for a group plan.
- `group_design` creates + redirects, is idempotent, 404s a foreign group, requires POST + login.
- The designer opens a coach's group plan (200, group payload in the page) and 404s a foreign
  coach's; autosave works for the group coach and 403s a foreigner; deliver + agent 400 a group
  plan. The group-detail page shows Design vs Open by whether a shared plan exists.
- Seed: the demo group gets a shared program (rooted at the group, with a scaffold), not
  duplicated on reseed.
