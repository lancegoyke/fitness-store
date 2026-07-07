# Meso — guided demo onboarding tour (opt-in, per-feature sample data)

Status: **COMPLETE** — all 5 phases shipped 2026-07-07 (PRs [#432](https://github.com/lancegoyke/fitness-store/pull/432), [#433](https://github.com/lancegoyke/fitness-store/pull/433), [#434](https://github.com/lancegoyke/fitness-store/pull/434), [#435](https://github.com/lancegoyke/fitness-store/pull/435), [#436](https://github.com/lancegoyke/fitness-store/pull/436)). Tracking issue: [#430](https://github.com/lancegoyke/fitness-store/issues/430).

Sibling of the [public sandbox demo](../../meso/public-sandbox-demo-plan.md) (which this
reshapes) and the [walkthrough video](../../meso/demo-walkthrough-video-plan.md). The
real-coach half extends the one-click demo from the
[first-time-UX plan](./first-time-ux-plan.md).

## Why

Landing on `/meso/demo/` silently mints a coach whose workspace is **already
full** — five athletes, a group, a built program, a logged session — because
`create_sandbox()` calls `demo.load_demo()` eagerly (`meso/sandbox.py:56`). The
visitor's first reaction is "where did all these clients come from?" There's no
walkthrough, nothing highlights what to click, and no action is tied to the data
that appears. A real new coach hits the same "magic data" wall from the
empty-state "Get started" card (`meso/roster.html:32-63`), whose only affordance
fires the same monolithic load.

The fix: the workspace **starts empty**, and an **in-app guided tour** walks the
user feature-by-feature, letting them **opt into each segment of data as they
learn that feature** — so data appears *because they asked for it*.

## Decisions (locked with owner)

| # | Decision | Choice |
|---|----------|--------|
| O1 | **Audience** | Both the anonymous `/meso/demo/` sandbox **and** real new coaches with an empty workspace. Same *tour*; the **data steps differ** (O4/O5). |
| O2 | **Tour style** | **In-app guided tour** — spotlight/coach-mark steps overlaid on the real screens, advancing with Next; each step offers an "add sample data" action. |
| O3 | **Data granularity** | **Per-feature segments** — each step loads only its slice (athletes → program → deliver → results → group), not one monolithic dump. |
| O4 | **Sandbox data** | The anonymous demo loads the **fake `is_demo` seed data** per segment (Maya/Devon/…, `SAMPLE_PLAN`, `SAMPLE_LOG`, `GROUP`). |
| O5 | **Real-coach data** | A real coach is guided to **add *themselves* as an athlete** (self-coaching) and program for themselves — **no fake demo athletes**. The self-athlete link is **non-billable**. |
| O6 | **Skip path** | Keep a **"skip · load everything"** shortcut that fires the existing aggregate `load_demo` and marks the tour complete — the tour is the default, never a wall. |
| O7 | **Loaded-state** | Per-segment "is it loaded?" is **derived from data** (mirror `has_demo`), not stored; only the tour's step index + dismissed/complete flag persist. |

## How it works

**Start empty, then populate per step.** `create_sandbox()` stops eager-loading;
the tour drives population. `load_demo` splits into idempotent, `is_demo`-scoped
**segment loaders** (reusing the existing `_ensure_*` helpers in `demo.py`):

| Segment | Loads | Reuses | Depends on |
|---|---|---|---|
| `athletes` | 5 demo athlete users + active demo links | `_ensure_demo_athlete`, `_ensure_demo_link` | — |
| `program` | Maya's "Hypertrophy Block" mesocycle tree | `_ensure_demo_plan`, `_build_plan_tree` | `athletes` |
| `delivery` | mark Maya's current week delivered | (part of `_ensure_demo_log`) | `program` |
| `log` | logged Lower session + refreshed 1RM | `_ensure_demo_log` | `program` |
| `group` | Strength Squad + shared plan + per-athlete overrides | `_ensure_demo_group` | `athletes` |

Each loader ensures its own prerequisites (idempotently), so steps are safe out
of order. `load_demo` becomes a thin "load all segments" wrapper — the O6 skip
path and the existing `demo_load` view keep working unchanged.

**The tour (both audiences share the steps; only the data action differs):**

1. **Welcome · Roster** — "Where your clients live." Sandbox → *Add 5 sample
   athletes*. Real coach → *Add yourself as your first athlete* (non-billable
   self-link).
2. **Athlete profile** — contraindications, history, plans.
3. **Program Designer** — the flagship week grid. → *Load a sample mesocycle* /
   *Build a program for yourself*.
4. **Deliver** — push the week to the athlete's phone (PWA/offline).
5. **Results** — logged sets, adherence, estimated 1RM.
6. **Groups** — shared programming + per-athlete auto-adjusts (skippable for a
   solo real coach).
7. **Adapt · AI agent** — sandbox: explain + signup gate; real trial coach: it
   actually drafts.
8. **Finish** — sandbox: "Create a free account"; real coach: "Invite your first
   real athlete." Sandbox offers "Remove demo data"; the self-athlete stays.

**Real-coach self-athlete — the one new billing rule.** A coach programming for
themselves must not burn a paid seat:
- Add `is_self` on `CoachAthlete` (mirror `is_demo`, `models.py:263`).
- `billable()` is `active().exclude(is_demo=True)` today (`models.py:185-193`);
  make it also `.exclude(is_self=True)`. Seat counting (`active_seat_count` →
  `billable().count()`, `billing/access.py:141-147`) and the over-limit freeze
  then treat the self-athlete as free, exactly like a demo link.
- **Verify `coach == athlete` is allowed** on one `CoachAthlete` row (same
  `User` both sides) — not done today; check uniqueness/validation and that
  roster/profile/deliver/athlete views render sanely. Cap at **one** self-link.
- The self-athlete is the coach's real account, so normal delivery/notification
  is fine (no `.invalid` address / opt-out — that's a demo-athlete thing).

**Tour progress** persists per user (step index + dismissed/complete) so it
survives reloads and cross-page navigation. Loaded-segment state stays derived
from data (O7).

## Landmines → mitigations

| Landmine | Mitigation |
|---|---|
| Empty sandbox with no tour = worse than today | The empty-start flip ships **with** the tour (Phase 2), never before it. Phase 1's refactor keeps eager-load on. |
| Segment loaders run out of order (user jumps steps) | Each loader ensures its prerequisites idempotently; `program`/`log`/`group` pull `athletes` first. |
| Self-athlete silently billed | `is_self` excluded from `billable()`; assert in tests that adding yourself doesn't change `active_seat_count`. |
| `coach == athlete` breaks a view | Audit roster/profile/deliver/athlete-home rendering; enforce single self-link; migration + tests. |
| Tour state lost on navigation (multi-page steps) | Server-persisted step index, not client-only. |
| Sandbox side effects (email/push) during data steps | Reuse the model-layer delivery guarantee in `demo.py`; segment endpoints are `is_demo`-scoped and sandbox-safe. |
| Front-end tour vs. PWA service worker / CSP | Self-contained asset (no CDN); reconcile with the SW precache (`views.py:1328` already references `meso_onboarding.js`). |
| Spotlight tours are rough on mobile | Bottom-sheet step cards on narrow viewports; the deliver/athlete step is phone-first. |

## Phases (PR-sized)

0. **Self-athlete + non-billable seat** *(standalone, shippable alone)* — ✅
   shipped, PR [#432](https://github.com/lancegoyke/fitness-store/pull/432) —
   `is_self` on `CoachAthlete` + migration; `billable()` exclusion; allow/validate
   `coach == athlete` (single self-link); minimal "Add yourself as an athlete"
   affordance on the roster; audit roster/profile/deliver rendering. Tests:
   self-link doesn't move `active_seat_count`; over-limit freeze ignores it.
1. **Segment the demo loaders** *(behavior-preserving refactor)* — PR [#433](https://github.com/lancegoyke/fitness-store/pull/433) —
   split `load_demo` into `athletes`/`program`/`delivery`/`log`/`group` loaders +
   `has_*` predicates; the existing `demo_load` view now accepts an optional
   `segment` POST field (no URL change). `load_demo` stays the aggregate;
   `create_sandbox` **still eager-loads** so nothing changes for users yet.
   Pure plumbing + tests.
2. **Sandbox tour** *(the visible change)* — PR [#434](https://github.com/lancegoyke/fitness-store/pull/434) — tour engine
   (hand-rolled coach-mark driver `meso_tour.js` + step state persisted on
   `CoachProfile.tour_state`); `create_sandbox` flipped to **empty-start**;
   the tour auto-starts; each step's action posts its segment to `demo_load`
   (returning to the page it came from via a safe `next`); **skip ·
   load-everything** (`tour_skip`) and **dismiss** controls.
3. **Real-coach tour** — PR [#435](https://github.com/lancegoyke/fitness-store/pull/435) — the self-coaching variant of the steps
   (variant derived from `is_sandbox`, never stored; typed per-step actions:
   `roster_add_self`, `plan_create` for yourself, agent draft when
   `can_use_agent`); the empty-state "Get started" card becomes the tour
   entry ("Start the guided tour", demo load kept as the secondary; original
   card returns once dismissed/completed). *(Depended on Phase 0 + 2.)*
4. **Analytics + polish** — PR [#436](https://github.com/lancegoyke/fitness-store/pull/436) — funnel events server-side on a meso-local
   `TourEvent` model (the `analytics` app turned out to be GA-script-only — no
   event mechanism to reuse; owner reads via admin/shell for now, dashboard is a
   follow-up); tour-marked (`tour=1`) attribution for self actions; mobile
   bottom-sheet steps (CSS-only, safe-area insets); aria-live step
   announcements + heading focus; `prefers-reduced-motion`.

## Key files & pointers

- Demo data: `app/store_project/meso/demo.py` (`load_demo`/`clear_demo`/`has_demo`,
  `_ensure_*`).
- Sandbox: `app/store_project/meso/sandbox.py` (`create_sandbox:37-57` — drop the
  eager `load_demo` in Phase 2; `expire_sandboxes` unchanged).
- Seed constants: `app/store_project/meso/management/commands/seed_meso_demo.py`
  (`ATHLETES` / `SAMPLE_PLAN` / `SAMPLE_LOG` / `GROUP`).
- Billing: `app/store_project/meso/models.py` (`CoachAthlete.billable:185-193`,
  `is_demo:263`); `app/store_project/meso/billing/access.py`
  (`active_seat_count:141-147`, over-limit freeze `:176`).
- Views/routes: `app/store_project/meso/views.py` (`sandbox_enter:780-823`,
  `RosterView`, SW precache `:1328`); `meso/urls.py` (`demo_load`/`demo_clear`
  `:95-96`).
- Templates: `templates/meso/roster.html` (empty-state card `:32-63`, demo banner
  `:19-31`), `templates/meso/_meso_base.html` (sandbox banner `:91-105`).
- Context: `meso/context_processors.py` (`sandbox_status` → `is_sandbox`).

## Acceptance criteria

- A fresh `/meso/demo/` sandbox lands on an **empty** roster with the guided tour
  started; no data is pre-loaded.
- Each tour step introduces a feature and offers an opt-in action that populates
  **only that segment**; segment loaders are idempotent, `is_demo`-scoped,
  prerequisite-safe, and trigger no outbound email/push.
- The **skip · load-everything** path reproduces today's fully-populated
  workspace and marks the tour complete; `clear_demo` still removes all demo data
  in one click.
- Real new coaches get the same *tour*, but the data steps guide them to add
  **themselves** as an athlete and program for themselves — no fake demo athletes.
- A coach's **self-athlete link is non-billable** (`is_self` excluded from
  `billable()`), addable at most once, and `coach == athlete` renders correctly
  across roster / profile / deliver / athlete views.
- Tour progress survives reloads and cross-page navigation.
- Keyboard-accessible, screen-reader-friendly, usable on mobile; analytics
  capture the step funnel and per-segment opt-in.

## Risks / open

- Tour driver (hand-rolled Alpine vs. small vendored lib), where to persist
  progress, and dismiss-time behavior — settle in Phase 2 with code in front of
  us, under the CSP/service-worker constraints.
- `coach == athlete` may surface latent assumptions in views written for
  distinct users — Phase 0 audit de-risks the rest.
- Empty-start is the one user-visible regression risk; mitigated by shipping it
  only alongside the tour (Phase 2), never in the Phase 1 refactor.
