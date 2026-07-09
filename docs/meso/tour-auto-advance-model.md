# Meso guided tour — auto-advance model (decision note)

Status: **decided + implemented** (#449, squash `a53bd55`, 2026-07-09). Written for
issue #441 P3 to settle the consistent advance model the audit asked us to nail down
"before patching steps individually"; shipped a follow-up PR later. This is now a
reference for the *live* behavior, not a pending plan.

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

**Accepted consequence (as shipped):** the self `deliver`/`results` controls post via `fetch`
(no full-page reload), so their mounted tour card only re-reads the advanced step on the
coach's *next* navigation, not the instant the fetch returns. The advance itself is correct and
resumes at the right step; making the card update immediately would require the client-side
advance this section rules out, so it was intentionally left as-is. The sandbox action steps
(and the self welcome/designer, which redirect) reload, so they advance visibly at once.

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

## Outcome (shipped in #449)

Implemented as the single coherent change this note prescribed: `advance_if_on_step` generalized
to every action-completion POST site (`demo_load` covers all sandbox steps via
`step_key_for_segment`; `roster_add_self` / `plan_create` / `plan_deliver` /
`athlete_log_session` / `group_create` for the self variant), plus self-variant completion
predicates for deliver/results/groups. Two refinements over the sketch above, forced by
shared-endpoint edge cases (a coach also coaches other athletes; the logger can save a
`pending` draft; a sandbox coach can self-link; an ended self-link's plans are archived):

- **Self predicates scope to `_active_self_working_plan(user)`** — the active self-link's
  `working_plan()` (which already excludes archived + group-materialized trees), not a broad
  `is_self` filter. `has_log` additionally requires a `done` log. A `has_group` predicate was
  added alongside the `has_delivery`/`has_log` named here, for symmetry.
- **Every self-action advance is gated on `variant_for(user) == "self"`** and on the step's own
  completion predicate (`advance_self_step_if_complete`), so a shared endpoint only ever
  advances the coach's *own* tour, in the right variant. The sandbox tour advances solely via
  `demo_load`.

Closed issue #441 (its final item). See the "Accepted consequence" note under *Why not
client-side advance* for the one deliberate UX tradeoff.
