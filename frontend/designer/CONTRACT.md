# Designer island architecture contract

Binding contract for Phase 2 PR B (`docs/meso/designer-framework-plan.md`,
Decisions 1+3; scratchpad `phase2-spec.md` "PR B" / `phase2-inventory.md`).
This is the spec the next agent (red RTL suites) encodes and a later agent
implements against. It is not code — no hook or component below exists yet
except `DesignerRoot`'s current PR-A placeholder
(`frontend/designer/src/DesignerRoot.tsx`), which this port replaces.

Ported pure logic already lives in `frontend/designer/src/lib/` (`api.ts`,
`agent.ts`, `override.ts`, `oneRm.ts`, `grid.ts`, `coachmarks.ts`, `keys.ts`,
`deliver.ts`) — every hook below is a thin, stateful wrapper around those
modules plus `fetch`. `Id` below means `number | string` (server ids are
numeric; a couple of fixture-era tests used string ids — kept permissive).

## Hook inventory

Each hook is a plain `function useX(...): {...}` — no context provider is
specified; `DesignerRoot` composes all of them and threads the results down
as props (see "Component tree"). Two ownership rules apply project-wide,
called out inline where they bind:

1. **`usePlanData` is the sole owner of `program`/`weeks`/`phases`.** Every
   verb that changes the *shape* of the grid (a full re-serialize via
   `applyPlanData`, or a row-merge push like `addExercise`/`addDay`) lives
   there, even though the network call itself is a plain `apiPost`. This
   avoids two hooks racing to set the same array.
2. **Hooks that finish by patching one row** (`useOverrideEditor`,
   `useOneRmEditor`) never touch `program` directly — they call a
   `patchExercise(exId, patch)` callback that `usePlanData` hands them.

### usePlanData

```ts
type PendingDelete = { type: "day"; di: number } | { type: "week"; weekId: Id };

function usePlanData(
  planId: Id,
  csrf: string,
  initial: { program: Day[]; weeks: Week[]; phases: Phase[]; viewing: Id | null; history?: HistoryState },
  identity: { athlete: AthleteIdentity | null; group: GroupIdentity | null },
): {
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewedWeekId: Id | null;
  history: HistoryState;
  athlete: AthleteIdentity | null; // hydrated once, never changed by applyPlanData
  group: GroupIdentity | null;     // hydrated once, never changed by applyPlanData
  pendingDelete: PendingDelete | null;
  setPendingDelete: Dispatch<SetStateAction<PendingDelete | null>>; // handed to useDeletes

  applyPlanData(data: PlanEnvelope): void;
  adoptHistory(data: HistoryCarrier): void;
  patchExercise(exId: Id, patch: Partial<Exercise>): void;
  updateExerciseField(dayIndex: number, exIndex: number, field: keyof Exercise, value: string): void;
  addExercise(dayIndex: number): Promise<void>;
  addDay(): Promise<void>;
  switchWeek(weekId: Id): Promise<void>;
  addWeek(): Promise<void>;
  setCurrentWeek(weekId: Id): Promise<void>;

  // derived (useMemo off program/weeks/phases/viewedWeekId)
  currentWeek: Week | null;
  viewedWeek: Week | null;
  weekIsViewed(w: Week): boolean;
  viewedIsCurrent: boolean;
  currentPhase: Phase | null;
  cycleLabel: string;
  weekHeading: string;
  blockHeading: string;
  deliverHref: string; // lib/deliver.ts deliverHref(planId, viewedWeekId)
}
```

`applyPlanData(data)` is the central sink, faithfully ported: sets
`program`/`weeks`/`phases`/`viewedWeekId` (`data.viewing ?? null`) and
`history` (`data.history ?? EMPTY_HISTORY` from `lib/api.ts`) — it does
**not** touch `athlete`/`group` (the source never did either; those are
hydration-time-only). It **always** clears `pendingDelete` (`setPendingDelete(null)`)
in the same call — this is the "disarm on grid swap" behavior
(`meso_delete.test.js` "pendingDelete disarms on grid swap"), collapsed
into `usePlanData` itself instead of a cross-hook callback, since
`pendingDelete` state is created here (see rule 1's corollary: `useDeletes`
receives `[pendingDelete, setPendingDelete]` rather than owning the state —
documented as a deviation below).

`adoptHistory(data)` — if `data.history` is present, adopt it; otherwise a
no-op. Used directly by `useAutosave.persistRow`'s `.then()`, and by
`addExercise`/`addDay` internally after their row-merge POST.

`updateExerciseField` is the controlled-input write path: it updates
`program` immediately (every keystroke, matching Alpine's `x-model`) but
does **not** persist — `ExerciseRow`'s `onBlur` calls `useAutosave.persistRow`
separately (matching the source's decoupled `x-model` + `@change`). Same
split for `toggleLoadType`, which is NOT a `usePlanData` method — see
`useAutosave` below for why it lives there instead.

`addExercise(dayIndex)` / `addDay()` — POST, push the server's row into
`program` (day/exercise), `adoptHistory(data)`. `switchWeek`/`addWeek`/
`setCurrentWeek` all POST/GET then `applyPlanData(data)`, matching the
source 1:1 (all three no-op on a redundant `switchWeek` to the already-viewed
week, per `meso.test.js` "week switcher").

**Dropped from the port**: the `!this.live` fixture branches on every one of
these verbs (a local id via `exSeq`, splicing instead of POSTing). The
island only ever mounts against a real hydrated plan (see "Non-goals") —
RTL specs drive these hooks with a mocked `fetch`, not a fixture code path.

### useAutosave

```ts
function useAutosave(options: {
  planId: Id;
  csrf: string;
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void; // from usePlanData
  adoptHistory: (data: HistoryCarrier) => void; // from usePlanData
}): {
  persistRow(ex: Exercise): void; // fire-and-forget: no-op if ex.id == null
  toggleLoadType(ex: Exercise): void; // flips load_type via patchExercise, then persistRow
}
```

`persistRow(ex)` is a faithful, direct port: POSTs
`{name, sets, reps, load, load_type: ex.load_type ?? "abs", rpe, note}` to
`/meso/api/plan/<planId>/prescription/<ex.id>/`, `.then(adoptHistory)`,
`.catch(err => console.error("Autosave failed", err))`. It is deliberately
**not** awaited by callers (`ExerciseRow`'s `onBlur` fires it and moves on),
matching the source's fire-and-forget autosave.

`toggleLoadType` is scoped here rather than in `usePlanData` because in the
source it's one atomic "flip + immediately persist" call
(`toggleLoadType(ex) { ex.load_type = ...; return this.persistRow(ex); }`)
with no intervening controlled-input state — it isn't a per-keystroke edit
like the grid cells, so it doesn't need `usePlanData`'s `updateExerciseField`
detour. It still respects rule 1 by writing through the injected
`patchExercise` instead of touching `program` itself.

### useDeletes

```ts
function useDeletes(options: {
  planId: Id;
  csrf: string;
  program: Day[];
  weeks: Week[];
  pendingDelete: PendingDelete | null;       // lifted from usePlanData
  setPendingDelete: Dispatch<SetStateAction<PendingDelete | null>>; // lifted from usePlanData
  applyPlanData: (data: PlanEnvelope) => void; // from usePlanData
}): {
  deleting: boolean;
  removeExercise(di: number, xi: number): Promise<void>;
  requestRemoveDay(di: number): void;
  requestRemoveWeek(weekId: Id): void;
  cancelPendingDelete(): void;
  confirmPendingDelete(): Promise<void>;
}
```

Faithful port of the Phase-0 delete verbs (`meso_delete.test.js`), with one
structural deviation flagged above: `pendingDelete` state itself is created
in `usePlanData` (so `applyPlanData` can clear it atomically alongside
`program`/`weeks`/`history` in one state update, no cross-hook effect
needed), and `useDeletes` is handed the state pair rather than owning it.
Every other detail is unchanged: `deleting` is one shared in-flight guard
across `removeExercise` and `confirmPendingDelete` (a `useRef<boolean>` or
equivalent synchronous guard, set **before** the awaited fetch so a
double-click can't race); `confirmPendingDelete` always clears both
`deleting` and `pendingDelete` in a `finally`, even on failure; day/week
delete POST to `session/<id>/delete/` / `week/<id>/delete/` and apply the
full envelope; `removeExercise` POSTs `prescription/<id>/delete/` and
applies the full envelope (it's a ✓ endpoint, not a row-merge).

### useUndoRedo

```ts
function useUndoRedo(options: {
  planId: Id;
  csrf: string;
  viewedWeekId: Id | null;
  history: HistoryState;
  applyPlanData: (data: PlanEnvelope) => void; // from usePlanData
}): {
  undoing: boolean;
  undo(): Promise<void>;
  redo(): Promise<void>;
}
```

`undo`/`redo` are a faithful port (`meso_undo.test.js`): no-op unless
`history.can_undo`/`can_redo` and not already `undoing`; POST
`{week_id: viewedWeekId}` to `undo/`/`redo/`; `applyPlanData(data)` on
success; `console.error` + swallow on failure; `undoing` cleared in
`finally`. **Keyboard wiring lives inside this hook**, not `DesignerRoot`:
a `useEffect` registers one `window.addEventListener("keydown", handler)`
for the hook's lifetime (cleaned up on unmount — the source's
`@keydown.window` binding on the root div is equivalent to a window
listener that lives as long as the page). The handler calls
`undoKeyIntent(event)` from `lib/keys.ts`; when it returns non-null, calls
`event.preventDefault()` then `undo()` or `redo()` — the `preventDefault`
call that `lib/keys.ts` deliberately left out (see that module's docstring)
happens here, and only on an actual undo/redo keystroke, never on a
no-op one.

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
  adoptHistory: (data: HistoryCarrier) => void;      // from usePlanData
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void; // from usePlanData
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

### useOneRmEditor

```ts
interface OneRmEditorState {
  ex: Exercise;
  value: string;
  saving: boolean;
  error: string;
}

function useOneRmEditor(options: {
  planId: Id;
  csrf: string;
  isGroup: boolean;
  adoptHistory: (data: HistoryCarrier) => void;
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void;
}): {
  oneRm: OneRmEditorState | null;
  openOneRm(ex: Exercise): void;
  updateValue(value: string): void;
  closeOneRm(): void;
  saveOneRm(): Promise<void>;
}
```

`openOneRm(ex)` no-ops unless `!isGroup && ex.load_type === "pct"` — a
group plan's rows use the override editor instead. Seeds
`value: ex.one_rm || ""`. `closeOneRm` guards mid-save like `closeOverride`.
`saveOneRm` runs `parseOneRm(value)` from `lib/oneRm.ts`; on `{ok:false}`
sets `error = "Enter a positive number, or leave blank to clear."`; on
success POSTs `{value: parsed.value}` to `prescription/<id>/one-rm/`, then
`patchExercise(ex.id, {one_rm: data.one_rm || "", one_rm_source: data.source || ""})`
and closes; on failure, `console.error` +
`error = "Couldn't save that 1RM. Please try again."`, stays open, row
unchanged.

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
│       ├── WeekGrid (view === "week")
│       │   └── DayCard[] (one per program day)
│       │       └── ExerciseRow[] (one per exercise)
│       ├── BlockView (view === "block")
│       └── AthletePreview (view === "athlete")
└── OverrideModal (rendered when override !== null; portal-free, fixed overlay like the source)
```

`WeekStrip` (week switcher chips + add/make-current/remove/undo/redo) is
NOT a sibling of `WeekGrid` — in the source it's the strip of controls
directly above the week grid, inside the same "week view" block, and only
rendered `x-show="live && weeks.length"`. It is its own component
(`WeekStrip`) mounted at the top of `WeekGrid`'s render when `view ===
"week"`, not a top-level tree entry, to keep `WeekGrid` focused on the
grid itself. The 1RM editor has no standalone component in the tree — it
renders inline inside `ExerciseRow` (a controlled input + save/cancel,
exactly where `oneRm.ex.id === ex.id` shows it in the source) since it's
always anchored to one specific row.

### DesignerRoot

No props (mounted directly by `main.tsx` into `#meso-designer-root`, same
as PR A). Hydrates once on mount:

- `#meso-plan-data` → `JSON.parse` → `{plan, group, athlete, program, weeks, phases, viewing, history}`. Missing/unparseable → render `null` (the no-op-without-a-plan guard, ported from `init()`'s early return — the bare designer URL redirects server-side before this ever matters in practice).
- `#meso-chat-thread` → `JSON.parse` → `ChatMessage[]`; empty/absent → the default greeting (group-aware: a different opening line when `group` is present, ported from `init()`'s override).
- `#meso-csrf` → `data-token` attribute → `csrf: string`.
- `#meso-designer-flags` → `{is_sandbox, can_use_agent, agent_allowance, signup_url, price_summary}` (see below) → passed straight to `ChatPanel`.

Composes every hook above (wiring `patchExercise`/`adoptHistory`/
`applyPlanData`/`pendingDelete` between them per the ownership rules), owns
`mode`/`view`/`periodStyle`/`checks` as local `useState` (see "View-state
rules"), and renders the tree. No prop drilling helper (no context
provider) — `DesignerRoot` passes hook slices straight down; the tree is
shallow enough (3-4 levels) that this stays readable.

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

### WeekStrip

Props: `{ weeks, viewedWeekId, viewedIsCurrent, pendingDelete, deleting, history, undoing, onSwitchWeek, onAddWeek, onMakeCurrent, onRequestRemoveWeek, onCancelPendingDelete, onConfirmPendingDelete, onUndo, onRedo }`.
Rendered only when `weeks.length > 0` (the source's `x-show="live &&
weeks.length"`; `live` itself is dropped per "Non-goals"). Renders the week
chips (each: label + a live-week dot), "+ Add week", the "Make current"
affordance (hidden when `viewedIsCurrent`), the remove-week
arm/confirm/cancel dance, and the undo/redo buttons (`disabled` off
`undoing`/`history.can_undo`/`history.can_redo`, `title` from
`history.undo_label`/`redo_label`). Testids: `week-chip-{id}`,
`add-week-button`, `make-current-button`, `remove-week-button`,
`confirm-remove-week-button`, `cancel-remove-week-button`, `undo-button`,
`redo-button`.

### WeekGrid / DayCard / ExerciseRow

`WeekGrid` props: `{ program, isGroup, pendingDelete, deleting, onRequestRemoveDay, onConfirmPendingDelete, onCancelPendingDelete, onAddDay, ...WeekStrip props }`.
Renders `WeekStrip`, the grid coachmark (`coachmarkVisible("grid")`/
`dismissCoachmark("grid")` from `useCoachmarks`, passed down), the
group-mode banner, one `DayCard` per `program` entry, and "+ Add day"
(testid `add-day-button`).

`DayCard` props: `{ day, dayIndex, isGroup, pendingDelete, deleting, onRequestRemoveDay, onConfirmPendingDelete, onCancelPendingDelete, onAddExercise, ...ExerciseRow passthrough }`.
Renders the day header (name/bias/count + remove-day arm/confirm/cancel,
testids `remove-day-{dayId}`, `confirm-remove-day-{dayId}`,
`cancel-remove-day-{dayId}`), the column headers, one `ExerciseRow` per
exercise, and "+ Add exercise" (testid `add-exercise-{dayId}`).

`ExerciseRow` props: `{ ex, dayIndex, exIndex, isGroup, unit, oneRmOpenForRow, oneRmEditorState, onFieldChange(field, value), onCommit(), onRemove(), onToggleLoadType(), onOpenOverride(), onOpenOneRm(), onOneRmChange(value), onOneRmSave(), onOneRmCancel() }`.
Controlled inputs for name/sets/reps/load/rpe/note (`onChange` →
`onFieldChange`, `onBlur` → `onCommit`, matching the `x-model` + `@change`
split noted under `usePlanData`); the load-type toggle button (`loadSuffix`
from `lib/grid.ts`); the group-mode "+ adjust"/`ex.adj` badge
(`onOpenOverride`); the individual %1RM badge/inline editor
(`onOpenOneRm`/`onOneRmChange`/`onOneRmSave`/`onOneRmCancel`, shown only for
`!isGroup && ex.load_type === "pct"`); the remove-exercise `×` (`onRemove`,
`disabled={deleting}`). Testids: `exercise-name-{exId}`,
`exercise-sets-{exId}`, `exercise-reps-{exId}`, `exercise-load-{exId}`,
`exercise-load-type-{exId}`, `exercise-rpe-{exId}`, `exercise-note-{exId}`,
`exercise-remove-{exId}`, `override-badge-{exId}`, `one-rm-badge-{exId}`,
`one-rm-input-{exId}`, `one-rm-save-{exId}`, `one-rm-cancel-{exId}`,
`one-rm-error-{exId}`.

### BlockView

Props: `{ phases, weeks, periodStyle, onSetPeriodStyle, onSwitchWeek }`.
Renders the macro strip, then one of the three period styles (timeline /
ladder / calendar) per `periodStyle`; the calendar cells use `cellStyle`/
`cellOn` from `lib/grid.ts` (with the default `sessionDays`, per that
module's documented decision). Testids: `period-style-timeline-button`,
`period-style-ladder-button`, `period-style-calendar-button`,
`block-week-{id}` (timeline bars, clickable → `onSwitchWeek`).

### AthletePreview

Props: `{ program, unit, checks, onToggleCheck }`. A pure, derived render
of the phone mock's first day/first-three-lifts view (`athleteDay`/`aTotal`/
`aDone` — computed as a `useMemo` inside this component or a small
`lib`-free helper co-located here, since they're view-shaping, not
network/state logic, and have no existing spec coverage to preserve).
Testid: `athlete-check-{k}` (`k` = the source's `"a0-{xi}-{i}"` key).

### OverrideModal

Props: `{ override, overrideHasExisting, unit, onSelectMember, onUpdateDraft, onClose, onSave, onClear }`. Rendered only when `override !== null`.
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
- **`view`** (`"week" | "block" | "athlete"`): default `"week"`. Set by
  `TopBar`'s "Preview as athlete" (→ `"athlete"`), `LeftRail`'s "Open plan →"
  (→ `"block"`), and the canvas segmented control (all three). No hook
  reads or writes it.
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
  hydrated `#meso-plan-data` payload (or renders nothing); RTL specs drive
  every hook/component with a mocked global `fetch`, never a parallel
  in-memory code path. Components are always "live" once rendered.
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
