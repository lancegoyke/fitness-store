# Meso — public, no-signup interactive sandbox (Level-2 demo)

Status: **PLANNED** — not started. Tracking issue: [#389](https://github.com/lancegoyke/fitness-store/issues/389).

## Why

The public Meso pages funnel every CTA to signup; there is no way to *try* the
product first. The owner's call: a **no-signup, fully interactive, ephemeral
sandbox** — a visitor lands in a populated coach workspace and can actually use
it. The **one** capability held back is the AI agent, which is gated behind
creating a real account. That gate does double duty: it keeps **all** agent
usage attributable/metered to a real account, and it is the natural conversion
moment.

Sibling of the [walkthrough video](./demo-walkthrough-video-plan.md) ("level 1").

## Decisions (locked with owner)

| # | Decision | Choice |
|---|----------|--------|
| S1 | **Model** | **Ephemeral per-visitor sandbox.** On entry, mint a throwaway coach `User` + `CoachProfile`, seed it via `load_demo()`, and **log the visitor in** as that account. No signup to enter. |
| S2 | **Interactivity** | **Fully interactive except the agent.** Build/edit programs, log sessions, explore roster/groups/designer — all real writes, scoped to the throwaway coach. |
| S3 | **Agent gate** | Running the AI agent **requires creating a real account.** In the sandbox, "Draft with AI" / agent actions route to signup ("Create an account to run the agent"). The sandbox **never** calls Anthropic — so agent usage is always tied to a tracked account. |
| S4 | **Side effects** | Email + web-push to athletes are **suppressed** for sandbox coaches; Stripe checkout/portal/trial are **hidden/blocked** (there is no real user to bill). Inviting/requesting a real email address is disabled in the sandbox. |
| S5 | **Expiry** | Sandbox accounts + data **auto-expire** after a TTL (recommend 24–48h) via a **django-q2** scheduled cleanup, mirroring the invite-expiry schedule. |
| S6 | **Carry-over** | **Deferred.** Phase 1: creating an account from the sandbox starts a **fresh** real workspace (sandbox work is not migrated). Migrating the in-progress program into the new account is a documented Phase 3 stretch. |
| S7 | **Isolation** | Each visitor is a distinct throwaway `User`; existing coach-scoping (`Plan.for_coach`, `editable_by`, etc.) isolates data by user for free. Sandbox users carry a marker so guards + cleanup can find them. |

## Why "ephemeral, logged-in throwaway coach" is the right shape

The seam research found ~20 login-gated views plus `request.user` scoping and
CSRF on every form. A **read-only anon** approach would need a custom auth layer
to special-case all of them. The ephemeral approach sidesteps that entirely:
because the visitor is *genuinely logged in* as the throwaway coach, every
existing `login_required` view, CSRF token, and scoping query **just works**. We
only add (a) sandbox creation/entry, (b) a handful of guards on the
side-effectful actions, and (c) expiry cleanup.

## How it works

**Entry — `GET /meso/demo/` (public):**
- If the session already has a live sandbox, resume it.
- Else create one: `User` (email `{uuid}@sandbox.invalid`, unusable/random
  password), `CoachProfile`, **sandbox marker**, `load_demo(user)`,
  `login(request, user)`, and record `expires_at` (session + a `SandboxSession`
  row). Then redirect to `/meso/` — which now renders the full workspace.

**Guards — a single `is_sandbox(user)` helper short-circuits:**
- **Agent** — `agent_propose()` (`views.py:2332`) and the `draft=1` path in
  `plan_create()` → `_reserve_plan_draft()` (`views.py:622`): return the **signup
  gate** instead of dispatching. (No Anthropic call — satisfies S3.)
- **Email/push** — `coach_invite()`, `coach_invite_resend()`,
  `athlete_request_coach()`, and `_notify_athlete_delivered()`
  (`views.py:1518/1580/1447/2264`, push at `:2279`): no-op for sandbox; and
  disable inviting arbitrary real emails in the sandbox UI.
- **Stripe** — `billing_subscribe()`, `billing_portal()`, `billing_start_trial()`
  (`views.py:2578/2619/2640`): hidden/blocked in sandbox.

**Expiry — django-q2 scheduled task:**
- A `SandboxSession` (or a `is_sandbox` marker + `created`) with `expires_at`; a
  scheduled job deletes expired sandbox users (cascade removes their data; or
  call `clear_demo()` then delete the user). Register via a data migration
  mirroring `meso/migrations/0018_register_invite_schedules.py`. See
  [`scheduling-plan.md`](./scheduling-plan.md).

## Landmines → mitigations (from seam research)

| Landmine | Mitigation |
|---|---|
| ~20 `login_required` views + `request.user` scoping | Visitor is really logged in as the throwaway coach → no special-casing. |
| CSRF on every POST form | Real session → tokens issued normally. |
| Agent API cost | Never called in sandbox (S3). |
| Email/push leakage | Demo athletes already opt out + `.demo.invalid`; block real-email invites/requests for sandbox coaches. |
| Stripe | Hide/disable billing surfaces for sandbox. |
| Bot abuse / DB growth | Rate-limit sandbox creation (per IP/session), short TTL, cap concurrent sandboxes, unusable password, no possible outbound email. |
| Marking sandbox users | Add a small **`SandboxSession`** model (OneToOne user, `created`, `expires_at`, source ip) rather than overloading `is_demo` (which is relationship/group-scoped, not user-scoped). |

## Phases

1. **Spine** — `SandboxSession` model + marker; `/meso/demo/` create+seed+login;
   `is_sandbox()` guards on agent/billing/invites/email; UI: agent buttons →
   signup gate, hide billing, a persistent "You're in a live demo — create an
   account to run the agent / keep your work" banner; `expires_at`.
2. **Cleanup + hardening** — django-q2 expiry task (mirror `0018_register_
   invite_schedules`), rate-limit creation, concurrent-sandbox cap.
3. **Carry-over (deferred, S6)** — migrate the visitor's sandbox work into a
   newly created real account at signup.

## Surfacing

- A public **"Try the demo — no signup"** button on `templates/meso/landing.html`
  and `templates/meso/become_coach.html` (both already public), linking to
  `/meso/demo/`.
- Homepage surfacing is **deferred** per owner.

## Key files & pointers

- Routes/views: `app/store_project/meso/urls.py`, `app/store_project/meso/views.py`
  (`RosterView:259` splits anon→landing; agent `:2332`; email `:1518/1580/1447/2264`;
  Stripe `:2578/2619/2640`; `demo_load:703`).
- Demo data: `app/store_project/meso/demo.py` (`load_demo`/`clear_demo`, `.demo.invalid`).
- Models: `app/store_project/meso/models.py` (`CoachProfile:46` has `created`;
  `CoachAthlete.is_demo:239`, `MesoGroup.is_demo:1782`; scoping `Plan.for_coach`/
  `editable_by` `:993–1040`).
- Scheduling precedent: `meso/migrations/0018_register_invite_schedules.py`,
  `agent/jobs.py` (`async_task` from `django_q.tasks`).

## Acceptance criteria

- A logged-out visitor hits `/meso/demo/` and lands in a populated, interactive
  coach workspace with **no login**.
- They can create/edit a program and log a session; changes persist for the
  session's lifetime.
- Any agent/AI action shows a **signup gate** and **never** calls Anthropic.
- No email, push, or Stripe side effects occur for sandbox coaches.
- Two concurrent visitors get **isolated** workspaces.
- Sandbox data **auto-expires** and is cleaned up on schedule.

## Risks / open

- Carry-over deferred (S6) — decide before Phase 3.
- Abuse/DB growth — TTL + rate limit + cap are the controls.
- "Real writes by an anonymous visitor" — the hard invariant is that a sandbox
  coach can **never** send email or be billed; guard at the view layer, not just the UI.
