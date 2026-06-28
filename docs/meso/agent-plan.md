# Meso — agent slice plan

**Status:** Phase 1 done & merged (PR #280, squash `953d9d4`; deployed) · Phase 2 done & merged
(PR #282, squash `ee7d456`; deployed) · Phase 3 built (branch `meso-agent-phase3`) · created 2026-06-27 ·
**next = agent Phase 4 (execution + eval)**
**Companion to:** [`decisions.md`](./decisions.md) (B6) · [`persistence-plan.md`](./persistence-plan.md)
**Goal of this slice:** replace the designer's canned agent-chat engine
(`detectIntent`/`applyIntent` in `meso.js`) and the review screen
(`mockdata.PROPOSED_CHANGES`) with a **real Claude proposal engine behind the
existing human review gate**. The agent writes `ProposedChange`s; the coach still
approves. Safe by construction — the human gate already exists and proposals are
inert until a later phase applies them.

### Decisions this rests on (see `decisions.md` B6)
- **Provider = Claude** (project standing guidance). Model pinned against the
  `claude-api` reference at build time: `claude-opus-4-8`.
- **Shape:** structured **tool-calling** — the model emits a validated batch of
  program edits (swap / progress / volume / deload), applied server-side.
- **Grounding:** the plan (serialized), the athlete's global contraindications,
  the coach's programming style + avoid-rules, and the coach's instruction.
- **Guardrails:** contraindications enforced in a **validation layer**, not just
  the prompt; **human-in-the-loop** approval (the review screen is that gate).
- **Eval:** golden cases so quality doesn't silently regress (later phase).
- **Execution:** sync for now; background job + streamed status deferred (Redis
  is already in the stack).

---

## Architecture

A new `store_project/meso/agent/` package, kept independent of the network in
tests by depending on a small client *interface* (`.propose(...) -> dict`):

```
meso/agent/
  client.py      MesoAgentClient — wraps the anthropic SDK; builds the request
                 (system prompt + grounding), forces the propose tool, returns
                 the tool input dict. get_default_client() reads settings.
  validation.py  clean_change() — the deterministic guardrail. Structural checks
                 (kind, target belongs to the plan) + a contraindication backstop
                 (a swap may not re-introduce a flagged movement), independent of
                 the prompt.
  service.py     propose_changes(plan, instruction, *, coach, client=None) —
                 grounds → calls the client → validates each candidate → persists
                 a ProposalBatch + accepted ProposedChange rows in a transaction.
```

The agent integration lives behind a tool with `tool_choice` forcing the
proposal tool (the structured batch). **Adaptive thinking is omitted** because a
forced `tool_choice` is incompatible with extended/adaptive thinking; the
proposal task is a single constrained extraction. Revisit (auto `tool_choice` +
adaptive thinking) when the agent becomes multi-turn/conversational.

The **system** prompt (stable coaching frame + tool contract) is sent with
`cache_control` (prompt caching); the per-plan grounding + instruction go in the
**user** turn (volatile), per the caching guidance.

### Data model

```
Plan ──< AgentProposalBatch (coach, instruction, summary, model, status)
            └──< ProposedChange (kind, session?, prescription?, day_label,
                                 title, before, after, rationale, honors,
                                 introduces_exercise, payload, status, order)
```

- `AgentProposalBatch` = one agent run behind the review gate.
  `status ∈ {pending, applied, dismissed}` (apply/dismiss land in Phase 2).
- `ProposedChange` = one proposed edit. `kind ∈ {swap, progress, volume,
  deload}` (mirrors the prototype's review badges). `session`/`prescription` are
  the structured targets (nullable; a volume change targets a session, a swap a
  prescription). `status ∈ {pending, approved, rejected}` (per-change approve
  persistence lands in Phase 2). `payload` is reserved for the apply step.

### Validation layer (the guardrail, deterministic + unit-tested)

`clean_change(raw, plan)` returns `(cleaned, errors)`:
1. **Structural** — `kind` is valid; a referenced `prescription_id` /
   `session_id` must resolve to a row *within this plan* (a foreign id is
   rejected, never silently applied); required display fields present + within
   length.
2. **Contraindication backstop** — `forbidden_terms(plan)` extracts the
   actionable "avoid" phrase from each **active** contraindication (the clause
   after an em/en dash, or after "avoid"/"no") down to its significant words; a
   swap whose `introduces_exercise` contains one of those terms is rejected.
   Conservative by design — the prompt does the nuanced reasoning, this is the
   deterministic backstop that runs regardless of what the model returned.

The service drops rejected candidates (logged on the batch) and persists only
clean ones, so a hallucinated or unsafe edit never reaches the review screen.

### Endpoint & review wiring

- `POST /meso/api/plan/<id>/agent/` — ownership-scoped via
  `_coach_plan_or_forbidden` (non-owner / inactive → 403; unknown plan → 404).
  Body `{"instruction": "..."}`. Runs the service synchronously and returns the
  batch + serialized changes. Returns **503** when no API key is configured (so
  the feature degrades cleanly in envs without Claude credentials).
- `GET /meso/review/<batch_id>/` — renders a **real** batch into the existing
  `review.html` (scoped to the requesting coach; foreign/unknown → 404). The
  bare `/meso/review/` stays on `mockdata.PROPOSED_CHANGES` until approve/apply
  lands (Phase 2).

### Settings

- `ANTHROPIC_API_KEY` (default `""` — optional, so the app boots and CI runs
  without it; the endpoint guards on its presence).
- `MESO_AGENT_MODEL` (default `claude-opus-4-8`).

---

## Phasing (one PR each)

**Phase 1 — Proposal engine. ✅ Done & merged (2026-06-27, PR #280).**
Models (`AgentProposalBatch`, `ProposedChange`) + migration + admin + factories;
the `agent/` package (client, validation, service); settings; the `POST .../agent/`
endpoint; read-only `review/<batch_id>/` wiring. Tests: validation unit tests,
service tests with a fake client (happy path + contraindication + structural
rejection), endpoint tests (ownership / login / method / missing-key guard /
persists), review-render test.
*Done when:* a coach instruction produces validated, contraindication-safe
`ProposedChange` rows the review screen can render. **No real apply, no chat UI.**

*Shipped* (branch `meso-agent-phase1`, **PR #280**, squash `953d9d4`; Django CI green, deployed
to Hetzner — migration `meso.0004` applied): models + `meso/agent/` (`client`/`validation`/
`service`) + `POST api/plan/<id>/agent/` (sync; 503 without an API key) + read-only
`GET review/<batch_id>/`. The validation guardrail enforces, server-side: valid kind, targets
resolving to the plan's **current week**, consistent session/prescription, a required target per
kind (swap/progress→prescription, volume→session, deload→none), and a **contraindication backstop**
that screens only swaps (plural-folded). Model pinned to `claude-opus-4-8`; **adaptive thinking
omitted** (incompatible with a forced `tool_choice`). Built red→green: 47 new tests (146 meso / 286
project-wide). **Local Codex review: clean (8 rounds)** — it caught two real bugs (a contraindication
bypass when `introduces_exercise` was omitted; `rationale` dropped on persist) plus a series of
guardrail-scoping refinements. **Deferred:** approve/apply, the chat rebuild, background job +
streaming, eval cases.

**Phase 2 — Review gate: approve/reject + apply. ✅ Done & merged (2026-06-27, PR #282, squash `ee7d456`; deployed).**
Persist per-change approve/reject on the real review screen; apply approved
changes back into the program (swap → set prescription name; progress → set
load; volume → set the prescription's set count; deload → flag the week); retire
`mockdata.PROPOSED_CHANGES`. `AgentProposalBatch.status` → applied/dismissed.

*Built:* `meso/agent/apply.py` (`apply_change`/`apply_batch`/`dismiss_batch`) applies a
change's structured `payload` — built deterministically by `agent.validation` from the tool's
`new_name`/`new_load`/`new_sets` fields (a swap falls back to the contraindication-checked
`introduces_exercise`). Endpoints (all scoped to a coach-owned batch, 404 otherwise; apply/dismiss
409 unless still pending): `POST api/change/<pk>/status/` (persist approve/reject),
`POST api/batch/<id>/apply/` (writes every **non-rejected** change in one transaction → batch
`applied`, bumps `Plan.modified`, returns the deliver URL), `POST api/batch/<id>/dismiss/`.
`review.html` now persists each toggle and wires Apply/Discard; the bare `review/` redirects to the
coach's latest pending batch (fixtures retired). No migration (status/payload already existed). Built
red→green: +33 tests (179 meso / 319 project-wide). *Done when:* a coach can approve/reject and
apply a real batch into the program. **No chat UI yet (Phase 3).**

**Phase 3 — Designer agent-chat column. ✅ Built (branch `meso-agent-phase3`).**
Rebuild the designer's left/agent column (`meso.js` `detectIntent`/`applyIntent`,
currently canned: swap-knee / lower-volume-d2 / progress / deload) to POST the
coach's message to `.../agent/` and render the returned batch inline, linking to
the review screen. Retire the canned intent engine.

*Built:* the canned keyword engine (`detectIntent`/`applyIntent`/`dispatch` in
`meso.js`, which matched the coach's text to one of four scripted edits and
mutated the in-memory grid in place) is gone. A coach turn — typed or via a chip
(chips now send their label verbatim as the instruction) — POSTs to
`api/plan/<id>/agent/` (the Phase 1 endpoint), and the returned batch renders
inline: a per-change list (`title` + `before`→`after`) under the agent's summary,
plus a **"Review N changes →"** link to the review gate. The agent only
*proposes* — the chat never mutates `program`/`weeks`/`phases`; changes stay inert
until the coach applies them at review. Friendly fallbacks for 503 (no key) / 502
(provider) / 400 / network errors; the composer + chips disable while the agent is
drafting (`agentTyping`). The fabricated seed thread is dropped for one orienting
greeting; the thread is **not persisted yet** (a later slice). Tests
(`test_designer_agent_chat.py`, red→green): no JS runner in-project, so they guard
the engine's retirement + the real endpoint wiring at the source level, plus a
render check. 192 meso / 332 project-wide pass; `ruff` + pre-commit clean. **Local
Codex review: clean (1 round, no findings).** *Done when:* a coach can chat the
agent into a real proposal batch and jump to review. **Deferred:** persisted chat
thread, background job + streamed "drafting…" status (Phase 4).

**Phase 4 — Execution + eval.**
Background job + streamed "drafting…" status (Redis); golden eval cases; logged
sessions fed into grounding.

## Out of scope (later)
Athlete-facing surfaces · groups · the full "changes since last delivery" diff UI.
