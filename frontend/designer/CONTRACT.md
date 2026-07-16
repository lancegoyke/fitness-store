# Designer island architecture contract

Originally the binding contract for Phase 2 PR B
(`docs/meso/designer-framework-plan.md`, Decisions 1+3; scratchpad
`phase2-spec.md` "PR B" / `phase2-inventory.md`) — the spec that port's RTL
suites encoded and its implementation was built against. That port shipped
long ago; this document is now a living reference for the *current*
architecture, kept up to date as the island evolves rather than archived,
because the next agent working in `frontend/designer/src/` still needs an
accurate map.

**Status (issue #455, phases A1–A5): single data owner.** The island
originally shipped with two sibling data owners — `usePlanData` (one week
at a time: `WeekStrip`/`WeekGrid`/`DayCard`/`ExerciseRow`) and a later,
separately-added `useGrid` (the P1 multi-week table, `MesoTable`) — hydrated
from two separate JSON payloads (`#meso-plan-data` / `#meso-grid-data`) and
kept loosely in sync by `DesignerRoot`. Phase A5 deleted the one-week owner
and every hook that only existed to feed it entirely
(`usePlanData`/`useAutosave`/`useDeletes`/`useUndoRedo`/`useReorder`/
`useOneRmEditor`/`useGridNav`, ~5,900 lines of source+spec) and the
components that rendered it (`WeekStrip`/`WeekGrid`/`DayCard`/
`ExerciseRow`). **`useGrid` is now `DesignerRoot`'s only data owner,
island-wide** — `#meso-grid-data` is the only hydration payload, and
`MesoTable` is the only exercise-grid rendering surface. Sections below
that document retired hooks/components are marked **RETIRED** and kept
brief (what replaced them + where), rather than deleted outright, since
"why does `useGrid.ts`'s header comment keep saying `mirrors useAutosave`"
is a real question a future reader will have. Full history of the P1–P5
multi-week build lives in `docs/archive/meso/fixed-selection-plan.md`
(archived, all 6 phases shipped); A1–A5's own history is issue #455 and its
linked PRs.

Ported pure logic lives in `frontend/designer/src/lib/` (`api.ts`,
`agent.ts`, `override.ts`, `grid.ts`, `coachmarks.ts`, `keys.ts`,
`deliver.ts` — `oneRm.ts` was deleted in Phase 2a with the %1RM editor) —
every surviving hook below is a thin, stateful wrapper
around those modules plus `fetch`. `Id` below means `number | string`
(server ids are numeric; a couple of fixture-era tests used string ids —
kept permissive); it's exported from `hooks/useGrid.ts` now (every other
hook that used to export it, `usePlanData`, is gone).

## Hook inventory

Each hook is a plain `function useX(...): {...}` — no context provider is
specified; `DesignerRoot` composes all of them and threads the results down
as props (see "Component tree"). Two ownership rules apply project-wide,
called out inline where they bind:

1. **`useGrid` is the sole owner of `grid`/`history`.** Every verb that
   changes the *shape* of the grid (add/remove day|week|exercise, reorder,
   move, undo/redo) awaits its POST then calls `refetchGrid()` — a plain GET
   that re-syncs the whole `grid` object in one `setGrid`. This is the same
   rule Phase 2 PR B originally wrote for `usePlanData` (retired below); A5
   just moved which hook it binds to, once there was only one hook left to
   bind it to.
2. **Verbs that finish by patching one cell** (`useOverrideEditor`'s
   `saveOverride`/`clearOverride`, `useGrid`'s own `patchCell`/
   `renameExercise`/`writeCellLine`/`patchRowColumns`) don't always go
   through a full `refetchGrid()` — cell-scoped edits patch just that cell
   in local state (`patchCellAdj` for an override reply, or the optimistic
   write built into the verbs themselves) so the UI repaints instantly
   without waiting on a full-grid network round trip.
   `useOverrideEditor` itself is data-owner-agnostic (see below) — it
   writes through whatever `patchExercise`-shaped callback its caller
   injects (`DesignerRoot` injects `gridState.patchCellAdj`).

### useGrid

The island's sole data owner (`frontend/designer/src/hooks/useGrid.ts`,
~650 lines — full shapes there, not reproduced here since the source is
the single source of truth for something this size). Owns one `grid:
MesoGrid | null` and `history: GridHistory`, hydrated once from
`#meso-grid-data` by `DesignerRoot.readHydration()`. Exposes:

- **`patchCell`/`renameExercise`**: optimistic + fire-and-forget, mirroring
  the retired `useAutosave`'s semantics below — local state updates
  immediately, the POST isn't awaited by the caller, and a failure is
  `console.error`'d rather than rolled back. Each in-flight write is
  tracked in `pendingWritesRef` so `fillAcrossWeeks` can flush them first
  (fill copies the source cell's already-committed DB values, so an
  in-flight edit must land first or it'd read stale data). Phase 2a:
  `patchCell`'s only patchable field is `text` — the cell IS one freeform
  string now (`GridCellPatch = Partial<Pick<GridCell, "text">>`).
- **`writeCellLine(exerciseSlotId, weekId, line, text)`** (Phase 2a): upserts
  one freeform (week × line) sub-line of a row's stack — addressed by
  slot/week/line, not pk, since the line may not exist yet (POST
  `row/<slot>/cell/` `{week_id, line, text}`, the server's `cell_line_write`
  get_or_create). Same optimistic fire-and-forget shape as `patchCell`;
  line 0 updates `cell.text` locally, blank text clears a line in place.
- **`patchRowColumns(exerciseSlotId, {tempo?, rest?, note?})`** (Phase 2a,
  D2): the per-exercise Tempo/Rest/instructions row columns — attributes of
  the block-shared ExerciseSlot (POST `row/<slot>/`, the server's
  `exercise_slot_patch`). Same optimistic fire-and-forget shape.
- **RETIRED in Phase 2a: `setOneRm`** — the %1RM editor is gone (a % load
  is just prescription text now; see "RETIRED: useOneRmEditor /
  RowOneRmEditor" below), and with it `GridCellOneRmPatch`.
- **`patchCellAdj`**: a pure local repaint, no POST of its own — `useOverrideEditor`
  already POSTed to `prescription/<id>/override/` and hands back
  `{adj, adjusts, history}`; `DesignerRoot` routes its `patchExercise` here.
- **Structural verbs** (`addDay`/`removeDay`, `addWeek`/`removeWeek`/
  `setCurrentWeek`, `addExercise`/`removeExercise`, `reorderDays`/
  `reorderExercises`/`moveExerciseToDay`, `undo`/`redo`, `skipCell`/
  `fillAcrossWeeks`/`addExerciseThisWeek` — `swapCell` retired in Phase 2a:
  a substitution is sub-line text now, written through `writeCellLine`):
  await their POST, then call `refetchGrid()` (a plain GET) to re-sync the
  whole grid in one `setGrid`. One shared in-flight guard (`busy`) across
  every structural verb so a double-click can't race two refetches.
  `fillAcrossWeeks` copies the WHOLE text stack (line 0 + sub-lines) of the
  source week to every other week server-side.
- **`refetchGrid`'s field whitelist**: the GET response is narrowed to
  `{mesocycle, weeks, days, history}` plus the A5-added `{plan, group,
  athlete, phases}` — every field `MesoGrid` carries must be explicitly
  listed here or it silently drops on the next refetch (regression-tested:
  `useGrid.test.ts` "refetchGrid carries the full payload through").

One group-only cell patch type — `GridCellAdjPatch` (`{adj, adjusts}`) — is
the grid analog of the retired `usePlanData`'s `patchExercise(exId, patch)`
on the one-week path (`GridCellOneRmPatch` went with `setOneRm` in Phase 2a).

### RETIRED: usePlanData / useAutosave / useDeletes / useUndoRedo

Issue #455 phase A5 deleted these four hooks (and their specs) outright —
`useGrid` above absorbed every responsibility they had:

- **`usePlanData`** owned `program`/`weeks`/`phases`/`viewedWeekId`/
  `history`/`athlete`/`group`/`pendingDelete` for the one-week view, with
  `applyPlanData` as its central re-serialize sink and `switchWeek`/
  `addWeek`/`setCurrentWeek` as its week-management verbs. `useGrid.grid`
  (now carrying `plan`/`group`/`athlete`/`phases` too, added in A5 step 1)
  and its `addWeek`/`removeWeek`/`setCurrentWeek` replace it 1:1; the
  concept of a single "viewed week" is gone — `MesoTable` renders every
  week as a column simultaneously, so there's nothing to switch between.
- **`useAutosave`** persisted one exercise row's fields (fire-and-forget,
  POST on blur) and flipped `load_type`. `useGrid.patchCell`/
  `renameExercise` are the per-cell equivalent (see above); the load-type
  toggle is `useGrid`'s own atomic-flip verb, same "commits immediately on
  click, not per-keystroke" behavior.
- **`useDeletes`** owned the one-week grid's day/week/exercise
  arm-then-confirm delete dance, sharing `pendingDelete` state lifted from
  `usePlanData`. `MesoTable` owns this UI itself now — its own local
  arm/confirm slot per day/week/exercise (see `MesoTable.tsx`'s header
  comment on `data-grid-restore`) calls `useGrid`'s remove verbs directly,
  no shared cross-hook pending-delete state needed since there's only one
  table, not a table-plus-sibling-view to keep disarmed in sync.
- **`useUndoRedo`** owned `undo`/`redo` plus the window `keydown` listener
  that wired them to the keyboard. `useGrid.undo`/`.redo` are the verb
  equivalent; the keyboard wiring was extracted into its own small hook,
  **`useUndoKeyboard`** (`hooks/useUndoKeyboard.ts`, ~20 lines) — a
  stateless `useEffect` that registers one `window.addEventListener`
  calling `undoKeyIntent` (`lib/keys.ts`, unchanged) and dispatching to
  whichever `undo`/`redo` callbacks it's given. `DesignerRoot` wires it as
  `useUndoKeyboard(gridState.undo, gridState.redo)` — no more view-conditional
  routing between two hooks' undo/redo, since there's only one now.

### useOverrideEditor

```ts
interface OverrideEditorState {
  ex: Exercise;
  members: GroupMember[];
  memberId: string;
  draft: OverrideDraft; // lib/override.ts
  saving: boolean;
  error: string;
}

function useOverrideEditor(options: {
  planId: Id;
  csrf: string;
  group: GroupIdentity | null;
  adoptHistory: (data: HistoryCarrier) => void;      // caller-injected — see below
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void; // caller-injected — see below
}): {
  override: OverrideEditorState | null;
  overrideHasExisting: boolean; // lib/override.ts overrideHasExisting(override.ex, override.memberId); false when override is null
  openOverride(ex: Exercise): void;
  selectOverrideMember(memberId: string): void;
  updateDraft(patch: Partial<OverrideDraft>): void;
  closeOverride(): void;
  saveOverride(): Promise<void>;
  clearOverride(): Promise<void>;
}
```

`openOverride(ex)` no-ops (leaves `override` null) unless `group` is
non-null and has members — faithful port of the "no-op outside group mode
or with no members" spec. Member selection on open picks the first member
whose id appears in `ex.adjusts` (falls back to `members[0].id`) — ported
verbatim from the source's `adjusted`/`memberId` logic — then seeds
`draft` via `overrideDraft(ex, memberId)`. `selectOverrideMember` re-derives
`draft` the same way and clears `error`. `closeOverride` no-ops while
`saving` (guards the mid-save dismiss the source protects against — Escape
and a backdrop click both route through it in `OverrideModal`).
`saveOverride` runs `parseOverrideLoadPct(draft.load_pct)`; on `{ok:false}`
sets `error = "Load % must be a whole number from 1 to 200."` and returns
without posting; on success POSTs the full diff
(`{athlete, swap, load_pct, sets, reps, note}`, all trimmed) to
`prescription/<id>/override/`. `clearOverride` POSTs
`{athlete: memberId, clear: true}`. Both funnel through the same internal
submit path: on success, `patchExercise(ex.id, {adj: data.adj ?? null,
adjusts: data.adjusts ?? []})`, `adoptHistory(data)`, close; on failure,
`console.error`, `error = "Couldn't save that adjust. Please try again."`,
editor stays open (`ex` on `program` is untouched either way, matching
"keeps the editor open ... row unchanged" in `meso.test.js`).

#### Per-athlete adjust on the multi-week table (`MesoTable`)

The broader multi-week grid subsystem (`useGrid`, `MesoTable`, `GridCell`/
`GridRow`/`GridDay`/`MesoGrid`) postdates this contract and is specified in
`docs/archive/meso/fixed-selection-plan.md` (archived — all 6 phases shipped)
— only the group-adjust slice is pinned here, since it reuses the override
machinery above.

`GridCell` (in `lib/api.ts`) gains two optional group-only fields, emitted
by `serialize_mesocycle_grid` on a GROUP plan for each cell that has an
effective adjust (individual plans never carry them, so `cell.adj` is
`undefined`): `adj?: string | null` — the cell's badge summary (e.g.
`"MO -10%"` / `"2 adjusts"`) — and `adjusts?: OverrideAdjust[]` — every
member's stored diff.

`MesoTable` gains `group: GroupIdentity | null` and
`onOpenOverride(row: GridRow, cell: GridCell)`. When `group` has members,
every NON-skipped cell renders the same `.meso-adjust-badge` /
`.meso-adjust-empty` "+ adjust"/`cell.adj` control (testid
`cell-override-badge-{prescription_id}`); a click hands `(row, cell)` up.
No control renders for an individual plan (`group === null`) or a skipped cell.

`DesignerRoot` synthesizes an `Exercise` from the `(row, cell)` pair
(`id: cell.prescription_id`, `name: row.name`, `text: cell.text`, plus
`adj`/`adjusts` — Phase 2a: no structured numbers left, so the override
draft's sets/reps fields seed from the member's stored adjust only, blank
otherwise) and opens `useOverrideEditor` — **its only instance now**
(issue #455 phase A5 deleted the one-week path's sibling instance, which
used to wire `patchExercise`/`adoptHistory` to the retired `usePlanData`) —
wired `patchExercise: gridState.patchCellAdj` (repaints the cell's
`adj`/`adjusts` in place, no grid refetch) and
`adoptHistory: gridState.adoptGridHistory` (adopts the reply's
`serialize_plan_history` into the grid's own undo history). `useGrid`
exposes `patchCellAdj(cellId, {adj, adjusts})` (a local, POST-free cell
repaint — the grid analog of the retired `usePlanData.patchExercise`) and
`adoptGridHistory` (coercing `string | null` labels so the override reply
adopts cleanly).

### RETIRED: useOneRmEditor / RowOneRmEditor

Issue #455 phase A3 moved the %1RM editor's network/patch logic onto
`useGrid.setOneRm`; A5 deleted the superseded `useOneRmEditor` hook and
left the inline editor in **`RowOneRmEditor`**, a module-private component
inside `MesoTable.tsx`. **Phase 2a (text-first cells) retired that too**:
with `load`/`load_type` gone from the cell, there is no typed `%` load left
for the front-end to gate a 1RM control on — a "75%" is just prescription
text, and resolving it against the athlete's 1RM is the server's job at
delivery/logging time (the athlete-side 1RM endpoints and
`coach_set_one_rm` survive unchanged backend-side). `RowOneRmEditor`,
`useGrid.setOneRm`, `GridCellOneRmPatch`, and `lib/oneRm.ts` (+ specs) were
all deleted.

### useAgentChat

```ts
interface ChatMessage {
  id: number;
  role: "agent" | "coach";
  text: string;
  changes?: ChatChange[];
  reviewUrl?: string | null;
  error?: boolean;
}

function useAgentChat(options: {
  planId: Id;
  csrf: string;
  initialMessages: ChatMessage[];   // hydrated from #meso-chat-thread, or the greeting default
  initialResumeUrl: string | null;  // the hydrated thread's last message's pollUrl, if it was still drafting
}): {
  messages: ChatMessage[];
  inputText: string;
  setInputText(value: string): void;
  agentTyping: boolean;
  chips: { label: string }[]; // static: the same 4 labels as the source, module-level constant
  threadRef: RefObject<HTMLDivElement>; // the scrollable thread container
  onInputKey(e: KeyboardEvent<HTMLInputElement>): void; // Enter → onSend (mirrors onInputKey)
  onSend(): void;
  onChip(label: string): void;
}
```

`send(instruction)`/`sendInstruction` collapse into one internal path used
by both `onSend` and `onChip`: push a `coach` message, `agentTyping = true`,
scroll, POST `{instruction}` to `agent/`; on non-2xx, push an `agent` error
message via `agentErrorText` (`lib/agent.ts`); on success, call
`pollBatch(data.status_url, {onMessage: pushAgent})` from `lib/agent.ts`
(no injected `fetchImpl`/`sleep` in production — the hook uses the real
`fetch`/`setTimeout`, only RTL specs inject fakes by mocking global `fetch`
directly, since `pollBatch`'s options are the hook's internal implementation
detail, not a prop); on a thrown network error, push the generic
"Something went wrong reaching the agent" message. `agentTyping` clears and
the thread re-scrolls in a `finally`, matching the source exactly.
`onSend`/`onChip` both no-op while `agentTyping` (can't double-submit).

**Resume-from-thread**: on mount, if `initialResumeUrl` is non-null, the
hook drops that placeholder message (it was still drafting when the page
was rendered) and immediately resumes polling it the same way — ported
from `hydrateThread`/`resumeDrafting`. `DesignerRoot` computes
`initialResumeUrl` from the hydrated `#meso-chat-thread` payload (the last
message's `pollUrl` field) since that's a one-time hydration decision, not
something `useAgentChat` should re-derive from `messages` on every render.

### useCoachmarks

```ts
function useCoachmarks(): {
  dismissed: Record<string, boolean>;
  coachmarkVisible(key: string): boolean; // !dismissed[key]
  dismissCoachmark(key: string): void;
}
```

On mount, seeds `dismissed` from `readDismissed(key)` (`lib/coachmarks.ts`)
for every key in `COACHMARK_KEYS`. `dismissCoachmark(key)` reassigns
`dismissed` (`{...dismissed, [key]: true}`, matching the source's
non-mutating update) and calls `dismiss(key)` best-effort — the note hides
in-page regardless of whether the storage write lands, exactly as today.

## Component tree

```
DesignerRoot
├── TopBar
├── (body, flex row)
│   ├── LeftRail
│   ├── ChatPanel
│   └── (canvas)
│       ├── (canvas header: view segmented control + periodStyle control)
│       ├── MesoTable (view === "table", default)
│       ├── BlockView (view === "block")
│       └── AthletePreview (view === "athlete")
└── OverrideModal (rendered when override !== null; portal-free, fixed overlay like the source)
```

Issue #455 phase A5 deleted `WeekStrip`/`WeekGrid`/`DayCard`/`ExerciseRow`
(the one-week-at-a-time tree they formed) entirely — `MesoTable` is the
canvas's only exercise-grid view now, and it isn't further decomposed into
child components the way the retired tree was: one file owns the whole
table (day sub-tables, week columns, row cells, drag handles, the 1RM
editor, the group-adjust badge), documented in prose in its own header
comment rather than a component-by-component breakdown here, since (unlike
the retired tree) there's no multi-file boundary left to document. The week
switcher (add/make-current/remove) is `MesoTable`'s own `WeekColumnHeader` +
toolbar, not a standalone component — every week renders as its own table
column, so there's no separate "switch to a week" verb left, only
"add/remove/make-current."

### DesignerRoot

No props (mounted directly by `main.tsx` into `#meso-designer-root`, same
as PR A). Hydrates once on mount:

- `#meso-grid-data` → `JSON.parse` → `MesoGrid` (`{plan, group, athlete, phases, mesocycle, weeks, days, history}`). **Required hydration gate** — missing/unparseable → render `null` (issue #455 phase A5: this is now the ONLY hydration payload; a plan with no mesocycle block at all is a documented "shouldn't happen post-scaffold" edge case — see `views.py`'s `MesoDesignerView.get_context_data` — that now renders a blank island instead of a degraded one-week grid, since there's no separate `#meso-plan-data` fallback left to render from).
- `#meso-chat-thread` → `JSON.parse` → `ChatMessage[]`; empty/absent → the default greeting (group-aware: a different opening line when `group` is present, ported from `init()`'s override).
- `#meso-csrf` → `data-token` attribute → `csrf: string`.
- `#meso-designer-flags` → `{is_sandbox, can_use_agent, agent_allowance, signup_url, price_summary}` (see below) → passed straight to `ChatPanel`.

Composes `useGrid` (the sole data owner), one `useOverrideEditor` instance
wired to it, `useTableReorder`, `useUndoKeyboard`, `useAgentChat`, and
`useCoachmarks`; owns `mode`/`view`/`periodStyle`/`checks` as local
`useState` (see "View-state rules"); derives `program` for `AthletePreview`
via `gridToProgram(grid, weekId)` and `cycleLabel` for `TopBar`/`LeftRail`
via `cycleLabelFromGrid(phases, weeks)` (both pure helpers in `lib/grid.ts`
added in A5 step 3 — the grid analogs of the retired `usePlanData`'s
`athleteDay`/`aTotal`/`aDone` view-shaping and `cycleLabel` memo). No prop
drilling helper (no context provider) — `DesignerRoot` passes hook slices
straight down; the tree is shallow enough (3-4 levels) that this stays
readable.

### TopBar

Props: `{ mode, onSetMode(mode), isIndividual, isGroup, athlete, group, cycleLabel, onPreviewAsAthlete, deliverHref }`.
Renders the Meso logo/back-link, the individual/group identity chip, the
mode segmented control, the cycle label chip, "Preview as athlete", and
(individual-only) "Review changes" + "Deliver"; group plans show the
"Deliver to all · soon" chip instead — ported 1:1 from the `x-show`
conditionals. Testids: `mode-individual-button`, `mode-group-button`,
`preview-athlete-button`, `deliver-link` (`href={deliverHref}`),
`review-link`.

### LeftRail

Props: `{ isIndividual, isGroup, athlete, group, phases, onOpenBlockView }`.
Renders the athlete/group identity block and the macrocycle phase list;
"Open plan →" calls `onOpenBlockView` (sets `view = "block"` in
`DesignerRoot`). Testid: `open-block-view-button`.

### ChatPanel

Props: `{ messages, agentTyping, chips, inputText, onInputChange, onInputKey, onSend, onChip, threadRef, flags: DesignerFlags }`.
Renders the thread (agent bubbles with inline `changes` + the review link,
coach bubbles, the typing indicator) and, below it, exactly one of three
gated footers derived purely from `flags` (no template conditionals — this
is the one region that used to be server-rendered `{% if %}` and is now a
plain client-side branch on hydrated data):

1. `flags.is_sandbox` → the signup CTA (`href={flags.signup_url}`).
2. `else if flags.can_use_agent` → the chip row + composer, and, when
   `flags.agent_allowance.metered`, the "`N of M runs left`" note (with a
   subscribe link when `flags.agent_allowance.tier === "free"`).
3. `else` → the exhausted-allowance gate: an upgrade CTA
   (`flags.agent_allowance.tier === "free"`, using `flags.price_summary`)
   or the plain "resets on the 1st" note otherwise.

Testids: `agent-review-link` (existing, unchanged), `agent-composer-input`
(existing), `agent-composer-send` (existing), plus new: `agent-chip-{n}`
(index-based — chip labels aren't guaranteed unique), `agent-sandbox-cta`,
`agent-upgrade-cta`, `agent-allowance-note`.

### RETIRED: WeekStrip / WeekGrid / DayCard / ExerciseRow

Issue #455 phase A5 deleted all four components outright — the multi-week
table (`MesoTable`, one `<table>` per training day, week columns across the
top) replaces the whole one-week-at-a-time tree they formed:

- **`WeekStrip`** (week switcher chips + add/make-current/remove/undo/redo)
  → `MesoTable`'s own `WeekColumnHeader` (per-column "Make current"/
  "Remove"/confirm-cancel controls) + its toolbar's "+ Add week" button.
  There's no more "switch to a week" verb (`onSwitchWeek`/week chips) —
  every week already renders as its own column, so there's nothing to
  switch between; undo/redo moved to `useUndoKeyboard` + `useGrid.undo`/
  `.redo`, wired at `DesignerRoot`, not a component.
- **`WeekGrid`** (one week's days, plus the "grid" coachmark) →
  `MesoTable` itself; its coachmark key is now `"table"` (A4's re-authored
  copy, not a mechanical port of the old "grid" mark — `COACHMARK_KEYS`
  dropped `"grid"` in A5 step 6).
- **`DayCard`** (one day's header + exercise rows) → `MesoTable`'s day
  sub-tables (one `<table>` per training day, rendered inline, not a
  separate component).
- **`ExerciseRow`** (one exercise's controlled inputs, per-field
  dirty-tracking, load-type toggle, override badge, inline %1RM editor) →
  `MesoTable`'s per-cell rendering, scoped PER CELL rather than per whole
  row — the dirty-tracking pattern carries forward (`MesoTable.tsx`'s
  header comment cites it as "ExerciseRow's dirtySinceFocus pattern").
  Phase 2a (text-first cells) then collapsed the cell's six structured
  inputs to ONE freeform text input (`cell.text`, via `useGrid.patchCell`)
  plus one input per sub-line of the cell's stack (`cell.lines`, upserted
  via `writeCellLine`) and a trailing ghost input that mints the next
  sub-line (max existing line + 1, or 1) on its first non-blank commit;
  Tempo/Notes/Rest moved off the cell onto per-ROW column inputs
  (`row.tempo`/`note`/`rest`, via `patchRowColumns`), matching the source
  spreadsheet's Exercise | Tempo | weeks… | Notes | Rest layout. The
  load-type toggle, the %1RM editor, and the one-week swap badge/menu are
  retired.

None of these four are exhaustively re-documented prop-by-prop here the way
they were before A5 — `MesoTable.tsx` (one file, ~1,500 lines, extensively
commented) is the source of truth for its own internal shape now that
there's no multi-file component boundary left to pin.

### BlockView

Props: `{ phases, weeks, periodStyle, onSetPeriodStyle, onSwitchWeek }`.
Renders the macro strip, then one of the three period styles (timeline /
ladder / calendar) per `periodStyle`; the calendar cells use `cellStyle`/
`cellOn` from `lib/grid.ts` (with the default `sessionDays`, per that
module's documented decision). Testids: `period-style-timeline-button`,
`period-style-ladder-button`, `period-style-calendar-button`,
`block-week-{id}` (timeline bars, clickable → `onSwitchWeek`).

`weeks: GridWeek[]` (issue #455 phase A5 — was `Week[]`, sourced from the
retired `usePlanData`; now straight off `gridState.grid.weeks`).
`GridWeek` already structurally satisfies `cellOn`/`cellStyle`'s
`Pick<Week, "current" | "deload">`, and gained its own `vol`/`inten`
fields (`serialize_mesocycle_grid` additions, A5 step 1) so the timeline's
`barH(w.vol ?? 0, 156)` bars don't silently render at the floor height — no
render-logic change in `BlockView.tsx` itself, only the prop type. **Real
behavior change, not just a wiring swap**: `onSwitchWeek` used to switch
which week the one-week view showed; with that view gone, `DesignerRoot`
wires it to `() => selectView("table")` (ignoring the clicked week's id —
a future "scroll that week's column into view" enhancement is a
nice-to-have, out of scope for A5) — clicking a timeline bar now just jumps
to the table, it doesn't scroll to that specific week.

### AthletePreview

Props: `{ program, unit, checks, onToggleCheck }` (plus optional
`coachmarkVisible`/`dismissCoachmark` for the "phone" coachmark). A pure,
derived render of the phone mock's first day/first-three-lifts view
(`athleteDay`/`aTotal`/`aDone`, computed as a `useMemo` inside this
component). Testid: `athlete-check-{k}` (`k` = the source's `"a0-{xi}-{i}"`
key). Component itself needed **zero** changes for A5 — only its caller
changed what it passes as `program`: `DesignerRoot` now derives it via
`gridToProgram(grid, weekId)` (`lib/grid.ts`, added in A5 step 3) — a pure
transform that walks `grid.days`, picks each row's cell at the resolved
week (default: `grid.weeks.find(w => w.current)`), and omits a row with no
cell for that week. Replaces the retired `usePlanData`'s hydrated `program`
array; no server round trip. Phase 2a: the derived `Exercise` is the new
text-first shape (`name` is just `row.name` — the one-week swap fields are
gone — plus `text`/`lines` off the cell and `tempo`/`rest`/`note` off the
row), and the phone mock renders the prescription text verbatim with ONE
loggable row per lift (no sets count left to fan set rows out from).

### OverrideModal

Props: `{ override, overrideHasExisting, onSelectMember, onUpdateDraft, onClose, onSave, onClear }` (`unit` dropped in Phase 2a — the header meta shows the shared prescription text verbatim instead of composed sets×reps·load). Rendered only when `override !== null`.
Backdrop click and Escape both call `onClose` (which internally guards on
`saving`, per `useOverrideEditor`). Testids: `override-member-{memberId}`,
`override-swap-input`, `override-load-pct-input`, `override-sets-input`,
`override-reps-input`, `override-note-input`, `override-clear-button`,
`override-cancel-button`, `override-save-button`, `override-error`.

## `meso-designer-flags` payload

A new `json_script` context payload the server view must add — replacing
the template's `{% if is_sandbox %}`/`{% elif can_use_agent %}`/`{% else %}`
composer gate with data the island renders client-side. Shape:

```ts
interface DesignerFlags {
  is_sandbox: boolean;
  can_use_agent: boolean;
  agent_allowance: {
    metered: boolean;
    allowance: number;
    remaining: number | null;
    can_use: boolean;
    tier: "unlimited" | "paid" | "free";
  };
  signup_url: string;   // reverse("meso:sandbox_signup")
  price_summary: string; // presenters.PRICE_SUMMARY, e.g. "$19/mo — unlimited athletes"
}
```

`is_sandbox`/`can_use_agent`/`agent_allowance`/`price_summary` already exist
in `MesoDesignerView.get_context_data` (`app/store_project/meso/views.py`)
or, for `is_sandbox`, in the `sandbox_status` context processor
(`app/store_project/meso/context_processors.py`) — which the view **cannot**
read from inside `get_context_data` (context processors apply later, at
render time). So the view must call `meso_sandbox.is_sandbox(self.request.user)`
itself (the same predicate the context processor calls) to populate this
dict, and add `signup_url: reverse("meso:sandbox_signup")` (currently
resolved by a template `{% url %}` tag, now needed as data). This is the
"view gains only this context dict — no logic change" the plan doc
promises — no new predicate, just one that used to be template-only now
also feeding a `json_script`.

`ChatPanel` renders the three server-gated states straight off this object
(see `ChatPanel` above) — the client makes no allowance/sandbox decisions
of its own; the object is the single source of truth, same as the
endpoint's own 402/403 gates stay the defense-in-depth backstop they are
today (the client gate is UX, not the enforcement).

## View-state rules

Local `useState` in `DesignerRoot`, all client-only (no server round trip
changes them):

- **`mode`** (`"individual" | "group"`): initial value is `"group"` if the
  hydrated plan has a `group`, else `"individual"` — ported from `init()`'s
  `if (this.group) this.mode = "group"` override of the `"individual"`
  default. Freely togglable afterward via `TopBar`'s segmented control;
  nothing else ever changes it programmatically.
- **`view`** (`"table" | "block" | "athlete"`): default `"table"` (issue
  #455 phase A5 — was `"week"`; the one-week view is gone and
  `#meso-grid-data` is now a required hydration gate, so there's no more
  "grid absent, fall back to week" branch to default around either). Set by
  `TopBar`'s "Preview as athlete" (→ `"athlete"`), `LeftRail`'s "Open plan →"
  (→ `"block"`), the canvas segmented control (all three), and `BlockView`'s
  `onSwitchWeek` (→ `"table"`, ignoring the clicked week's id — see
  "BlockView" above). No hook reads or writes it. `selectView` itself
  collapsed to a bare `setView(v)` in A5 — it used to also branch on
  `gridState.refetchGrid()` vs. `planData.reloadWeek(...)` to paper over the
  two-owner staleness problem (an edit made in one owner could leave the
  other stale on switch); with one owner, that problem doesn't exist.
- **`periodStyle`** (`"timeline" | "ladder" | "calendar"`): default
  `"timeline"`. Set only by `BlockView`'s own segmented control; irrelevant
  outside `view === "block"`.
- **`checks`** (`Record<string, boolean>`): the athlete-preview set-done
  toggles, keyed by the source's `"a0-{xi}-{i}"`. Local, ephemeral,
  never persisted — ported verbatim (`toggleCheck`).

## Non-goals

- **No fixture mode.** Every hook above talks to the real API
  unconditionally — there is no `live` flag, no `exSeq` id generator, no
  splice-instead-of-POST branch. `DesignerRoot` only ever mounts against a
  hydrated `#meso-grid-data` payload (or renders nothing — issue #455 phase
  A5, was `#meso-plan-data`); RTL specs drive every hook/component with a
  mocked global `fetch`, never a parallel in-memory code path. Components
  are always "live" once rendered.
- **Error handling is unchanged in kind**: every failure path stays
  `console.error(...)` + either an inert state (autosave: nothing visible
  changes) or an inline `error` string on the relevant editor's state
  (override/1RM). No toast library, no error boundary beyond React's
  default, no retry/backoff — a failed save just leaves the editor open for
  the coach to retry by hand, exactly as today.
- **Not ported** (confirmed dead by the inventory, already dropped from
  `lib/`): `accent`, `theme`, `onDeliver()`/`delivered` toast (unreachable —
  no template ever set `delivered`), `round25`, `exSeq`, every `!this.live`
  fixture branch, and `sessionDays` as *fixture state* (its shape survives
  as `cellOn`/`cellStyle`'s optional parameter with the same default value —
  see `lib/grid.ts`).
- Out of scope for this port entirely (per `phase2-spec.md`): no
  HMR/django-vite integration, no athlete-logger island, review/deliver/
  athlete/cardio pages stay Alpine, no visual redesign (design tokens are
  identical — the same CSS custom property names move onto the island's
  root), no new server endpoints beyond the `meso-designer-flags` context
  dict.
