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
  - **Phase 2b — create-group UI (built; this PR).** Create a brand-new group from the roster
    (name + focus + pick members), so groups no longer come only from the seed/admin.
- **Phase 3 — per-athlete overrides (the `adj` overlay) (built; this PR).**
  `PrescriptionOverride` per member (load %, swap, volume), effective-program resolution
  (`shared + overrides`), the designer's "Shared program · per-athlete auto-adjusts" row +
  per-row `adj` badge driven by real diffs.
- **Phase 4 — deliver to all members (built; this PR).** Delivering a group week fans out a
  per-athlete delivered snapshot (each member's *resolved* program), reusing the athlete
  surface + the delivery email/push from the athlete slice.

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

## Phase 2b — build notes

- **A group can finally be born from the product, not just the seed/admin.**
  `MesoGroup.create_for_coach(coach, *, name, focus="", athletes=())` is the model
  entry point: it creates the coach's group, then adds each athlete through the
  existing `add_athlete` so the same active-link tenancy guard applies — an
  athlete without an active link to this coach is *skipped*, not raised, because
  the create form only ever offers the coach's own athletes and a stale/foreign
  pick shouldn't fail the whole create. No migration (no schema change — just a
  helper + a view + a form).
- **Endpoint:** `group_create` (POST `/meso/group/new/`) is a plain form POST (not
  JSON), the roster's "New group" disclosure. `name` is required (a blank one
  creates nothing and routes back to the roster — the field is also `required`
  client-side); `focus` is optional; `athletes` is the multi-valued list of
  picked ids. `_coach_active_athletes` resolves the posted ids: it sanitizes each
  to a UUID (a malformed value is skipped, never reaching the ORM as a query
  error → no 500) and scopes to the coach's own *active* links, so only their
  current athletes resolve and a foreign/stale pick simply drops out. On success
  it lands on the new group's **detail page**, where the coach designs the shared
  program (Phase 2a) — the natural next step.
- **Roster:** the *Groups* card now always renders (was gated on `{% if groups %}`),
  with a group count, an empty state, and a `<details>` "New group" disclosure
  holding the form — name + focus inputs and a checkbox per roster athlete (the
  same `athletes` the *Individuals* card lists, already coach-scoped to active
  links). JS-free (a native `<details>`), so it needs no Alpine on the roster.

## Tests (Phase 2b)

`meso/tests/test_group_create.py` — model helper + the view + the roster form:

- `create_for_coach` creates a coach-owned group (name/focus/active status), adds
  the given athletes off their active links, defaults `focus` blank with athletes
  optional, and *skips* (doesn't raise on) an athlete with no active link.
- `group_create`: a POST creates the group with its picked members and redirects
  to the group detail; a foreign athlete / a pending-link athlete / a malformed
  (non-UUID) id are each ignored (no member, no 500); a blank name creates nothing
  and redirects to the roster; GET is 405; anonymous is redirected to login and
  writes nothing.
- The roster offers the create form (posts to `group_create`, a `name="athletes"`
  checkbox per athlete carrying the athlete id) and excludes another coach's
  athlete from the picker.

## Phase 3 — build notes

- **The overlay model.** `PrescriptionOverride(membership FK → GroupMembership, prescription FK
  → ExercisePrescription, swap_name, load_pct, sets, reps, note)` with `unique(membership,
  prescription)` — one adjust per member per shared lift. It hangs off the `GroupMembership`
  (so, transitively, the same `CoachAthlete` link that owns the member's individual plans — D-a),
  and a **same-group invariant** ties the override's prescription to the membership's group's
  shared program. Migration `meso.0009`.
- **A member's effective program = shared template + their diffs.** No second hierarchy — the
  override is a thin diff layered on the shared `ExercisePrescription`: `swap_name` replaces the
  name, `load_pct` scales a numeric load (rounded to 2.5, matching the designer's `round25`),
  `sets`/`reps` override the volume, `note` the note. RPE isn't per-athlete in this slice.
  `serializers.resolve_prescription(prescription, override)` is the pure resolver (used by the
  designer overlay now and deliver-to-all in Phase 4).
- **Helpers + tenancy.** `GroupMembership.set_override(prescription, **diff)` upserts (raising
  `InvalidTransition` on a cross-group prescription, dropping a no-op `load_pct=100`, and
  clearing instead of storing an empty diff — returning `None`); `clear_override(prescription)`
  drops it. `PrescriptionOverride.clean()` backstops the same-group rule on the admin (raw FKs),
  and `has_diff`/`has_diff_from` gate the empty-diff case.
- **The `adj` badge, driven by real diffs.** `serializers.group_adjustments(plan, prescriptions)`
  is one query over the plan's overrides scoped to the group's *active* members (an ended
  member's adjust drops off, like `active_member_users`); it maps each prescription to a per-row
  `adj` summary — one member's `"{initials} {label}"` (e.g. `MO -10%`), or `"N adjusts"` for
  several — plus an `adjusts` breakdown. `override_adj_label` folds a swap (`→ Box Squat`), a
  load delta (`-10%`/`+5%`), and a volume tweak (`2×8`) into the badge text. `serialize_plan`
  attaches `adj`/`adjusts` onto a group plan's grid rows (the group analogue of the individual
  plan's `last` column); `serialize_group_identity` members now carry their athlete `id` so the
  endpoint can target them.
- **Endpoint.** `prescription_override` (POST
  `/meso/api/plan/<plan_id>/prescription/<pk>/override/`) sets or clears one member's adjust.
  Group plans only (individual → 400); coach-scoped via `_coach_plan_or_forbidden` (403); the
  prescription must belong to the plan (404) and `athlete` must be an active member (400);
  `load_pct` is bounded (`MIN/MAX_LOAD_PCT`). Body: `{athlete, swap/load_pct/sets/reps/note}` to
  set, `{athlete, clear: true}` (or an empty diff) to clear; the reply carries the recomputed
  row `adj` so the badge repaints. **No in-grid override *editor* yet** — the badge renders off
  real diffs (seed/admin/API created); the click-to-adjust UI is the immediate follow-up.
- **Designer + seed.** The group grid's `adj` badge renders from the serialized diffs (with a
  per-athlete breakdown on hover); the "Shared program · per-athlete auto-adjusts" banner now
  reflects that the badges are live. The seeded demo group gets a few overrides (a load %, a
  contraindication swap → a "2 adjusts" row, and a volume tweak), idempotent across reseeds.

## Tests (Phase 3)

`meso/tests/test_group_overrides.py` — model + helpers + resolution + serializer + endpoint:

- Model: `unique(membership, prescription)`; `has_diff`; `clean` rejects a cross-group
  prescription; `__str__` names the athlete.
- `set_override` creates/updates, normalizes `load_pct=100` to a no-op, returns `None` (and
  clears) on an empty diff, and raises on a cross-group prescription; `clear_override` deletes /
  is a no-op.
- `resolve_prescription` applies a swap / load % (2.5-rounded, non-numeric load left alone) /
  volume, and yields the base for a `None` override; `override_adj_label` for each diff kind.
- `group_adjustments` shows initials + label for one member, `"N adjusts"` for several, excludes
  an ended member's override, and is empty with no overrides.
- `serialize_plan` emits `adj`/`adjusts` on overridden group-plan rows, never on un-overridden
  rows or an individual plan; the group payload members carry their athlete `id`.
- The endpoint: a coach sets / clears; 403 foreign coach; 400 individual plan / non-member
  athlete / bad `load_pct`; 404 foreign prescription; 405 GET; redirect when anonymous.
- Seed (`test_seed_demo.py`): the demo group gets per-athlete overrides, not duplicated on reseed.

## Phase 4 — build notes

- **The fan-out modeling — a per-member *materialized* individual plan.** The athlete surface
  (`/meso/me/`, the session logger) only ever reads **individual** plans' live delivered weeks
  (`Plan.objects.for_athlete` is individual-only; a group plan never reaches an athlete). So
  delivering a group week can't just stamp the shared plan — each member needs their *resolved*
  program (shared template **+** their override diffs) as live rows in an individual plan they
  own. Phase 4 **materializes** that: per active member, a `Plan` rooted at their `CoachAthlete`
  relationship whose current week mirrors the group's delivered week with `resolve_prescription`
  applied. The athlete then sees it through the exact same surface + delivery email/push the
  individual slice already built — no athlete-side change at all.
- **`Plan.source_group` — provenance, distinct from the root.** A new nullable FK records *which
  group a materialized individual plan was fanned out from*. It is **orthogonal** to the
  `relationship`-XOR-`group` root constraint (a materialized plan is rooted at a `relationship`
  like any individual plan; `source_group` is just where it came from). A partial
  `unique(relationship, source_group)` constraint (where `source_group` is set) makes the per-
  member plan a singleton — re-delivery refreshes the *same* plan, never spawns a second.
  Migration `meso.0010`.
- **Coach surfaces never see a materialized plan.** A materialized plan is rooted at the coach's
  own relationship, so without guarding it would leak into the coach's individual designer /
  deliver / results flows (and the bare-URL "working plan"). `PlanQuerySet.for_coach` and
  `editable_by` now both exclude `source_group`-rooted plans (`source_group__isnull=True`), so the
  coach manages the group *only* through the shared program; the derived snapshots are
  **athlete-facing only**. `for_athlete` is deliberately left unfiltered — the materialized plan
  is exactly what the athlete should see.
- **Materialization + re-delivery (log-preserving).** `GroupMembership.sync_delivered_plan(group_week)`
  get-or-creates the member's materialized plan and **syncs its current week in place** to the
  resolved group week: sessions matched by `day_number`, prescriptions by `order` (both via
  `update_or_create`, so a surviving row — and its `SessionLog`/`LoggedSet` — is *updated*, not
  recreated); sessions/prescriptions dropped from the shared program are deleted (a removed lift's
  logged sets `SET_NULL` off, a removed day's `SessionLog` cascades). Re-delivery therefore
  **propagates override/program edits** to the athlete while **preserving their logged work** for
  unchanged rows — the same "contents stay live" contract the individual flow has.
- **The fan-out.** `MesoGroup.deliver_current_week()` resolves the shared plan + its current week,
  then for each **active** membership (`active_memberships()`) syncs + stamps the member week
  (`delivered_at`) + writes a per-member `WeekDelivery` snapshot; it also stamps the shared (group)
  week as the coach-side record. Raises `InvalidTransition` when there's nothing to deliver (no
  shared plan / no week / no members). Returns `(now, [(member_plan, member_week), …])` so the
  caller notifies each athlete. No `request`/notify in the model layer (the seed reuses it).
- **Endpoints.** `plan_deliver` is now group-aware: for a group plan it runs the fan-out (was a
  Phase-2a `400` placeholder) and returns `201 {members, delivered_at}`; per-athlete email + push
  fire on `transaction.on_commit`, each best-effort, exactly as the individual deliver. The
  coach-facing entry is `group_deliver` (POST `/meso/group/<id>/deliver/`) — a plain form POST from
  the group-detail page's *Deliver this week to all N members* button; coach-scoped (foreign → 404),
  requires a shared program + members (else a flashed error), lands back on group detail with a
  success message. Both share `_fan_out_group_delivery` (maps `InvalidTransition` → the human
  message).
- **Seed.** The demo group's shared week is delivered once (idempotent — skipped when already
  stamped), so a fresh DB gives the three demo members a materialized, *resolved* delivered plan
  (Devon's −10% load, Priya's Box Squat swap, Marcus's volume tweak all visible on their athlete
  surface).

## Tests (Phase 4)

`meso/tests/test_group_deliver.py` — model fan-out + scoping + the endpoints:

- `sync_delivered_plan` materializes one plan per member (rooted at their relationship, tagged
  `source_group`), with the resolved week (a swap / load % / volume applied per member, the base
  for an un-adjusted member); re-running updates the *same* plan (no duplicate) and **preserves a
  `SessionLog`** logged between deliveries while **propagating an override change**; a prescription
  dropped from the shared program drops from the member week.
- `deliver_current_week` stamps each member week + the group week, writes per-member snapshots, and
  raises `InvalidTransition` for no shared plan / no week / no members.
- `for_coach` / `editable_by` exclude a materialized plan; `for_athlete` includes it; the member
  reaches the delivered session through the unchanged athlete surface (`/meso/me/` shows it, the
  session logger opens it).
- `plan_deliver` on a group plan fans out (`201`, `members` count), notifies each member once
  (email, with `django_capture_on_commit_callbacks`), and `400`s with no members / no week; a
  non-owner is `403`.
- `group_deliver`: a coach delivers from group detail (`302` back, members get plans, success
  message); a group with no shared program flashes an error and delivers nothing; foreign group →
  `404`; GET → `405`; anonymous → login redirect. The group-detail page renders the deliver button
  only with a shared program + members.
- Seed (`test_seed_demo.py`): the demo group's members get a materialized delivered plan, not
  duplicated on reseed.

## Phase 5 — the group agent (the AI agent edits the shared program)

Shipped 2026-06-30 (PR #350, no migration). The "What a group plan can't do yet"
note above is now **superseded for the agent**: `agent_propose` no longer `400`s a
group plan. The AI proposal agent edits the group's **shared program** behind the
same propose → review → apply gate every individual run uses. **Per-athlete
auto-adjust generation by the agent stays a later phase** — this is shared-template
editing (the simplest correct first slice; the conservative folded backstop keeps
it safe), consistent with how the group designer already works ("edit the shared
program directly").

- **Grounding (`agent.service.build_context`).** A group plan grounds on the
  *group* (a new `_group_context`): name/focus, each active member + their active
  contraindications, and the **folded** set across all active members. No
  single-athlete `recent_logs`.
- **Safety (`agent.validation.forbidden_terms`).** The contraindication backstop
  folds across **every active member** — a swap/add unsafe for *any one* member is
  rejected (the shared row trains everyone).
- **Apply (`agent.apply`) is unchanged** — it writes onto the shared
  `ExercisePrescription`, so every member inherits the edit.
- **Scoping.** The review/status/apply endpoints widened from `for_coach`
  (individual-only) to `editable_by`, so a coach reaches their group batches
  (identical set for individual plans — no regression). A group run is tagged
  `trigger=group` (usage ledger → attributed to the group, athlete null).
- **Post-apply routing.** A group batch routes back to the **designer** (a group
  has no individual deliver screen; delivery is deliver-to-all).
- **Designer UI.** The agent composer / coachmark / review-gate note now render in
  Group mode; the "group agent arrives in the next phase" placeholder is gone.

Tests: `test_group_agent.py`. **Deferred:** group agent **Phase 2** = per-athlete
auto-adjusts (the agent generates per-member `PrescriptionOverride`s instead of
editing the shared row).
