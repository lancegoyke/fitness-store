# Meso — persisted designer chat thread plan

**Status:** building (branch `meso-chat-thread`) · created 2026-06-28
**Companion to:** [`agent-plan.md`](./agent-plan.md) (the chat went live in agent
Phase 3, but the thread was never persisted) · [`decisions.md`](./decisions.md) (B6)
**Goal of this slice:** the designer's agent-chat conversation **survives a page
reload**. Today the chat is ephemeral — `meso.js` re-seeds `messages` to a single
orienting greeting on every load, so a coach who proposes changes, navigates away,
and comes back sees an empty thread even though the proposals themselves persisted.
This was the last loose end from the agent slice (Phase 3/4 both noted "the thread
is not persisted yet").

## The key realization — no new model, no migration

The conversation is **already persisted, losslessly**, in the
`AgentProposalBatch` rows the agent slice writes. Every coach turn maps 1:1 to a
batch:

- `batch.instruction` — the coach's chat message, verbatim (the endpoint stores
  `payload["instruction"].strip()`, which is exactly what `meso.js` sent).
- `batch.summary` + the batch's `ProposedChange` rows — the agent's reply.
- `batch.status` — `pending` / `applied` / `dismissed` / `failed` / `drafting`.
- `batch.created_at` — thread order.

The agent never emits free-form chit-chat: it only ever responds with a proposal
batch (a summary + changes) or an error. So the batches **are** the thread. A
dedicated `ChatMessage` table would store nothing the batch doesn't already hold;
it would duplicate `instruction`/`summary` and add a write path on every turn for
no new information. We therefore reconstruct the thread from the plan's batches
rather than adding a model — the same "reuse what exists, defer new tables" taste
the athlete slice followed (Phases 1–3 added no migration).

## Architecture

```
serializers.serialize_chat_thread(plan)
    → [ {coach msg}, {agent msg}, {coach msg}, {agent msg}, … ]   # oldest first
```

Each of the plan's `AgentProposalBatch` rows (ascending `created_at`) expands to
two messages, in the exact shape `meso.js`'s `messages` array renders:

- **coach** — `{id: "coach-<pk>", role: "coach", text: instruction}`.
- **agent** — depends on the batch's terminal state:
  - `failed`   → `{role: "agent", text: error-or-fallback, error: true}` (no changes).
  - `drafting` → `{role: "agent", text: "Still working on this proposal…"}` (a
    stuck/in-flight run after a reload — rendered as a neutral note, never silently
    dropped).
  - `pending` / `applied` / `dismissed` → `{role: "agent", text: summary-or-fallback,
    changes: [serialize_proposed_change…], reviewUrl: review_batch-url-when-changes}`.

`reviewUrl` is camelCase here (the serializer emits messages **ready to drop into
`messages`**, unlike the live status endpoint, which emits snake-case `review_url`
that `batchMessage` remaps). The change shape reuses `serialize_proposed_change`,
so a hydrated proposal renders byte-identical to a freshly returned one.

### View + template

`MesoDesignerView.get_context_data` adds `ctx["chat_thread"]`. The template
injects it with `{{ chat_thread|json_script:"meso-chat-thread" }}` inside the
existing `{% if plan_data %}` block (the thread only matters when a plan loads).

### JS (`meso.js`)

`init()` reads `#meso-chat-thread`; when present and non-empty it **replaces** the
default greeting (`this.messages = thread`). An empty history (no batches) keeps
the orienting greeting. Hydrated message ids are strings (`coach-<pk>`); runtime
turns keep appending with `Date.now()` — no key collision.

## Out of scope (later)
A `ChatMessage` model (only needed if the agent ever sends free-form text not
tied to a batch) · editing/deleting past turns · pagination of a very long thread
(every proposal batch is rendered; revisit only if a plan accumulates hundreds) ·
real-time multi-tab sync.

## Testing
pytest + factory_boy, mirroring the slice discipline:
- **serializer** — empty plan → `[]`; one pending batch → `[coach, agent]` with
  the instruction/summary text + changes + a review url; ordering (oldest first,
  interleaved); a `failed` batch → an error message, no changes/link; a
  no-changes batch → no review link + a summary fallback; **plan-scoping** (a
  batch on another plan never bleeds in).
- **view** — the designer page injects `meso-chat-thread` with the prior
  instruction; a plan with no batches injects an empty `[]`.
- **wiring** (source-level, as the project has no JS runner) — `meso.js` reads
  `meso-chat-thread` and hydrates `messages`, keeping the greeting fallback.
