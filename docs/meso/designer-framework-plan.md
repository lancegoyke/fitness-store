# Meso — designer React island + undo/redo, keyboard nav, drag-and-drop

## Why

The designer (`templates/meso/designer.html` + `static/js/meso.js`) has outgrown
"sprinkles." It is a stateful grid editor — 898 lines of component logic, a
637-line template carrying ~170 Alpine directives — and the next three features
on the roadmap are exactly the class Alpine is worst at:

- **Undo/redo** — needs a transaction model; Alpine offers none.
- **Grid keyboard navigation** — roving focus across cells, arrow-key movement,
  focus restoration after re-renders; deeply DOM-coupled work with no
  composition affordances to keep it sane.
- **Drag-and-drop reordering** — Alpine has no DnD story at all; the known
  pattern (SortableJS reconciled against `x-for`) is fragile.

Two existing properties make the migration cheap and low-risk:

1. **The logic is already framework-free.** `createMeso()` returns a plain
   object; the vitest suite (`frontend/*.test.js`) drives it with no Alpine
   runtime. The reactive layer is thin and replaceable.
2. **The server is already the source of truth.** The page injects the plan via
   `json_script` (`designer.html:23`), every mutation hits a JSON endpoint with
   session-cookie + `X-CSRFToken` auth, and structural changes swap state via
   `applyPlanData` from a full re-serialize. That contract survives any
   frontend unchanged.

## Decision 1 — React island on the designer page only

**React 19 + Vite, mounted into a root `<div>` in `designer.html`. Everything
else stays exactly as it is.** Django templates keep serving every page; the
designer template keeps its `{% extends %}`, topnav, `json_script` payloads,
and CSRF span — only the Alpine markup inside is replaced by a mount div and a
`<script type="module">`. Same-origin fetch means the existing session + CSRF
auth carries over verbatim; no DRF, no tokens, no CORS.

- **dnd-kit** is the deciding library: best-in-class accessible drag-and-drop
  with built-in keyboard sensors — Phases 3 and 4 partially collapse into one
  problem domain.
- **TypeScript** for the island (free under Vite; a grid editor with an undo
  stack and focus management is where types pay for themselves). The ported
  logic modules may stay `.js` initially — porting them as-is is fine.
- Scope: **designer only.** Review, deliver, athlete session, cardio, and the
  onboarding chrome stay Alpine indefinitely; `alpine.min.js` keeps shipping.

Considered and rejected: **full SPA + API-first shift** (nothing needs it — the
JSON API already exists as plain Django views; an SPA buys client routing we
don't want and an auth migration we don't need); **Vue** (its edge is
mechanical Alpine-template translation, which matters little since the
template's *structure* is being rewritten anyway; DnD ecosystem is weaker);
**Svelte** (fine DX, but no dnd-kit equivalent and a smaller ecosystem);
**staying on Alpine** (see Why); **htmx** (wrong model — the grid is
client-stateful, not swap-oriented; htmx stays where it already works).

## Decision 2 — undo/redo is a backend feature (snapshot op-log)

The designer autosaves every change; by the time a coach reaches for undo the
DB has already moved. A client-side undo stack would desync or have to replay
inverse API calls — and inverse-replay breaks precisely on the case that
matters (un-deleting a row recreates it under a new PK, orphaning every later
history entry that referenced the old one). So undo lives server-side:

- **`PlanAction` model** (new): `plan` FK, monotonically increasing `seq`, a
  short human `label` ("Deleted Day 3", "Applied agent batch"), and
  `snapshot` — a JSON dump of the plan's full editable state *before* the
  mutation (all weeks → sessions → prescriptions + overrides + the
  current-week pointer). Plans are a few KB serialized; snapshots are cheap.
  `serialize_week_snapshot` (`serializers.py:214`) is precedent — this is its
  plan-wide sibling. Cap retained history (e.g. last 50 actions per plan).
- **Every mutating designer endpoint records one action** before it writes:
  `prescription_patch`, `session_add_exercise`, `session_add`, `week_add`,
  `week_set_current`, `prescription_override`, `batch_apply` (one snapshot =
  the whole agent batch undoes as a single step), plus the #401 deletes and
  the Phase 4 reorders. **Excluded:** `coach_set_one_rm` (writes the athlete's
  own `AthleteOneRm` record, not plan structure — undoing another person's
  data from a plan-scoped history would be wrong).
- **`POST api/plan/<id>/undo/` and `/redo/`**: restore the top snapshot inside
  a transaction (undo pushes the *current* state onto a redo stack first;
  any new mutation clears the redo stack), then return the same re-serialized
  payload the week endpoints return, so the client just calls
  `applyPlanData`. The frontend's whole job is two buttons + Ctrl/Cmd+Z.

**Prerequisite: structural deletes must be soft.** `SessionLog.session` is
`on_delete=CASCADE` — hard-deleting a Session (or a Week above it) destroys
the athlete's logged history, and no snapshot can restore what the cascade
took. So `Week` / `Session` / `ExercisePrescription` get a `deleted_at`
timestamp; serializers and designer queries filter it; #401's delete endpoints
set it; snapshot-restore is then pure field updates + flag flips on stable
PKs — nothing is ever recreated. (`LoggedSet.prescription` is already
`SET_NULL`, so prescriptions are the least dangerous — but one mechanism for
all three levels keeps restore trivial.) Permanent reaping of long-deleted
rows is deferred.

Undo scope is per-plan, not per-user — correct while a plan has a single
editing coach, revisit if that changes.

## Decision 3 — build integration stays boring

- **Vite builds to stable filenames** in the static tree (e.g.
  `static/js/dist/designer.js` + `.css`), referenced with plain `{% static %}`.
  No `django-vite`, no manifest plumbing — WhiteNoise's manifest storage
  already content-hashes at `collectstatic`, same as every other asset.
- **Local dev:** `vite build --watch` alongside `just dev` (new `just
  frontend-watch` recipe). No HMR initially — one mental model, zero dev/prod
  template conditionals; revisit if rebuild latency ever hurts.
- **Docker:** a `node:22-slim` build stage (`npm ci` + `vite build`) whose
  `dist/` is `COPY --from`'d into the static dir before `collectstatic` in the
  existing `python:3.13-slim` image. A broken frontend build fails the image
  build, so it can never deploy.
- **CI:** `frontend.yml` already runs vitest on the right paths; add
  `tsc --noEmit` + `vite build` steps and the new frontend source dir to its
  path filters. "Django CI" (the deploy gate) is untouched.
- **Source layout:** the island lives in `frontend/designer/` next to the
  existing `frontend/*.test.js` suite; `package.json` grows `dependencies`
  (react, react-dom, dnd-kit) alongside the current dev-only tooling.

## Phases

Each phase is a separately shippable PR (or two); nothing below starts until
the phase above is verified.

### Phase 0 — deletes (#401) + deliver cleanup (Alpine) — ✅ shipped (#404, #405)

- Issue #401 as specced, with one amendment from Decision 2: the three delete
  endpoints **soft-delete** (`deleted_at`), which also resolves #401's open
  question about cascades and puts the rows where undo can restore them.
- Extract `deliver.html`'s inline `Alpine.data("mesoDeliver", …)` component to
  a `static/js/` module and bring it under the vitest suite (deliver stays
  Alpine forever, so this cleanup keeps its value regardless of the island).
- **Do not** otherwise refactor designer Alpine/template code — it is about to
  be replaced; polish nothing.

**Outcome (2026-07-02):** shipped as #404 (deliver extraction →
`static/js/meso_deliver.js`) + #405 (soft-delete slice, built red→green).
Review rounds grew the scope beyond the designer, all in #405 — a fresh
session should treat these as part of the soft-delete contract:

- The **athlete surface** filters live rows too (home, logger,
  `athlete_log_session`, `_clean_logged_sets`, `athlete_set_one_rm`): a
  removed delivered day/exercise disappears for the athlete and rejects new
  logs/1RMs; already-logged history keeps rendering.
- The logger's save replaces only live-prescription sets, so a hidden row's
  `LoggedSet`s survive the athlete's next save.
- `GroupMembership.sync_delivered_plan` **soft**-deletes member-side rows
  dropped from the source (previously a hard delete — the pre-existing
  `SessionLog` CASCADE hazard this plan flags) and revives them in place
  (`deleted_at: None` in the upsert defaults) when a source row returns.
- `week_delete`/`week_set_current` re-check row flags under the plan lock;
  the deliver screen (selector, `?week=`, session count) is live-only;
  `applyPlanData` disarms a pending index-anchored delete confirm.

### Phase 1 — undo/redo backend + minimal UI — 🔴 red suites committed

> Status (2026-07-02): the failing contract suites are committed on branch
> `meso-designer-phase-1-undo` (`test_designer_undo.py` +
> `frontend/meso_undo.test.js`); the full spec they encode is a comment on
> #403 ("Phase 1 spec"). Implement to green against them without modifying
> them; rebase the branch onto main first (it was cut pre-#405-squash).

- `deleted_at` migration + `PlanAction` model, snapshot serializer/restorer,
  action recording in the endpoints listed in Decision 2, `undo`/`redo`
  endpoints. The restorer runs in a transaction and asserts snapshot PKs all
  still exist (they must, given soft delete).
- Minimal Alpine UI: two toolbar buttons + Ctrl/Cmd+Z / Shift+Ctrl/Cmd+Z →
  endpoint → `applyPlanData` (~the size of `setCurrentWeek`). Throwaway by
  design; the island re-implements it in Phase 2.
- Tests: pytest round-trips — edit/undo/redo, delete-day/undo (logs intact!),
  batch_apply/undo, redo-stack cleared by a fresh edit, history cap.

### Phase 2 — the island (behavior-identical migration)

- Vite + React app in `frontend/designer/`; entry reads the same
  `meso-plan-data` / `meso-chat-thread` / `meso-csrf` elements; template
  swaps Alpine markup for the mount div + script tag. Server view unchanged.
- Port `createMeso()`'s logic into hooks/modules; the POJO helpers (polling,
  parsing, override drafts) move nearly as-is with their tests. Component
  tests: vitest + React Testing Library (jsdom already in devDeps).
- Inline styles move into real component CSS as part of the rewrite (design
  tokens from `base.css` / the locked design system, not new colors).
- Dockerfile node stage, `just frontend-watch`, CI additions per Decision 3.
- **No new features in this PR.** Verification checklist: grid edit autosave,
  week switch/add/make-current, override editor (group), 1RM editor
  (individual), agent chat + poll-resume, coachmarks, deliver link with
  `?week=`, undo/redo buttons, and the sandbox/demo plan at `/meso/demo/`.

### Phase 3 — keyboard navigation

- Roving tabindex across grid cells; arrow keys move focus, Enter commits /
  Escape reverts a cell, Tab follows the natural order; focus survives
  `applyPlanData` swaps (re-anchor by prescription id + column).
- Undo/redo shortcuts move from the Phase 1 shim into the real focus system.
- Fixes the a11y gaps the #401 review deferred (unlabeled inputs, focus rings
  killed by `outline:none`).

### Phase 4 — drag-and-drop

- dnd-kit: reorder exercises within a day, move exercises across days, reorder
  days within a week (weeks-reorder deferred). Keyboard sensor gives
  accessible DnD via the Phase 3 focus system.
- Backend: `Session.order` and `ExercisePrescription.order` already exist —
  add reorder endpoints (`POST …/session/<pk>/reorder/`-style, whole-list
  order arrays to keep them idempotent), each recording a `PlanAction`.

## Deferred

- HMR dev server / `django-vite` (only if `vite build --watch` feels slow).
- A second island for the athlete logger (nothing forces it; decide on demand).
- Coalescing rapid autosave edits into one undo step.
- Permanent reaping of soft-deleted rows (management-command sweep, later).
- Reordering weeks; collaborative/multi-coach editing (explicitly not planned).

## Pointers

- Designer component: `app/store_project/static/js/meso.js` (`createMeso`)
- Template + payload injection: `templates/meso/designer.html:17-25`
- Mutating endpoints: `app/store_project/meso/views.py:1927-2312`, `batch_apply` at `:2684`
- Serializers (snapshot precedent `serialize_week_snapshot`): `app/store_project/meso/serializers.py`
- Cascade hazard: `SessionLog.session` (`models.py:1460`, CASCADE); `LoggedSet.prescription` (`models.py:1730`, SET_NULL)
- FE tests + CI: `frontend/`, `.github/workflows/frontend.yml`
- Image: `Dockerfile` (single python stage today; node stage lands in Phase 2)
