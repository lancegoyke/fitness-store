# Meso guided tour — auto-advance model (decision note)

Status: **decided, implementation deferred.** Written for issue #441 P3. This records
the consistent advance model the audit asked us to settle "before patching steps
individually." No tour-flow code changes in the P3 PR that ships this note.

## The problem the audit named

The tour renders state but "never reacts to what the user actually does." Today advance is
almost entirely manual (the **Next** button). The *only* server-side auto-advance is
`tour.advance_if_on_step(user, "profile")`, called from `AthleteProfileView` (a page-visit
signal for step 2). Everything else relies on the coach clicking Next, even right after they
complete the step's action.

Existing predicates are uneven across variants:

| step | key | sandbox predicate (`loaded`) | self predicate (`loaded`) |
|------|-----|------------------------------|---------------------------|
| 0 | welcome | `has_athletes` | `has_self_link` |
| 1 | profile | — (visit-advance) | — (visit-advance) |
| 2 | designer | `has_program` | `has_plan` |
| 3 | deliver | `has_delivery` | **none** |
| 4 | results | `has_log` | **none** |
| 5 | groups | `has_group` | (static) |
| 6 | agent | — | (`has_plan` gates lock copy, no `loaded`) |
| 7 | finish | — (terminal) | — (terminal) |

Two gaps fall out of that table: the **self variant has no `has_delivery`/`has_log`
predicate at all**, and even where a predicate exists, `loaded` only flips the action
button to a done state — it never advances the step.

## The model

Classify each step by its **goal event**, then bind the matching signal:

1. **Visit-goal steps** — the goal is to *land on a page*. Only step 1 (profile).
   Signal: page GET while parked on the step → `advance_if_on_step`. Already implemented for
   profile; this is the pattern to generalize, not replace.

2. **Action-goal steps** — the goal is to *complete a data-producing action*: welcome,
   designer, deliver, results, groups, agent. Signal: the step's own `has_*` predicate
   flips true. Advance at the **action-completion site** (the POST handler that produces the
   data — the same `demo_load` / `roster_add_self` / `plan_create` / deliver / log endpoints
   that already record the opt-in), gated on `current_step_key(user) == <step>`, calling the
   generalized `advance_if_on_step(user, <step>)`.

   Advancing at the action site (not on a later page visit) is what makes the tour *react to
   what the user did* — the audit's through-line. It also means the organic twin and the
   tour button both advance, matching the funnel fix (P3-2) that already records the opt-in
   from either path.

3. **Terminal step** — step 7 (finish) does not advance.

### Why not client-side advance

Advance stays server-authoritative: the funnel events (`TourEvent`) and tour resumability
already live in `tour_state`. A client-side advance would desync the recorded funnel and the
resumed step. Keep the single source of truth on the server.

### The `current == step` guard prevents skips

`advance_if_on_step` only advances when the coach is parked *exactly* on the named step and
that step is not the last. Because step 2 (designer) creates the plan that step 6 (agent)
inspects, a coach who runs the tour in order is never mid-flight on step 6 when step 2's
action fires, so no step is skipped. Each action advances only its own step index.

## What the follow-up implementation must do (as one coherent unit)

1. **Fill the self-variant predicate gaps first.** Add self-variant `has_delivery` /
   `has_log` (the self athlete's plan delivery + a logged session), so deliver/results can
   action-advance in the self variant. Without these the deliver/results steps have no signal
   and must stay manual.
2. **Generalize the advance hook.** Call `advance_if_on_step(user, <step>)` from each
   action-completion site, right where the opt-in is recorded, for welcome / designer /
   deliver / results / groups / agent, gated on `current_step_key`.
3. **Keep the profile visit-advance** as the one visit-goal case.
4. **Leave finish terminal.**

Do this in a single change so the flow is coherent — not per-step patches — exactly as the
audit warned. Ship it behind the same red/green + browser-walkthrough verification the P1/P2
fixes used, because it changes the mainline tour progression.

## Decision for P3

Documented, **not implemented** in the P3 PR. The predicate-gap + action-hook work is a
focused follow-up (roughly: one presenter/predicate change per variant + one `advance_if_on_step`
call per action site + tests). Tracked under issue #441.
