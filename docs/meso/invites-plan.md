# Meso — N4 athlete onboarding / invites

The still-open foundation decision (N4 in [`decisions.md`](./decisions.md)): how an
athlete *joins* a coach. Phase 1 of persistence shipped a peer-invite **state
machine + tokened URLs** on `CoachAthlete` (`invite`/`request`/`accept`/`decline`/
`end`) and the `invite_accept`/`invite_decline` token views — but those require the
athlete to **already be a `User`**, and nothing in the UI ever *creates* an invite.
So in practice links are only ever seeded. N4 closes that gap: a coach can invite a
real person by email, that person signs up (or logs in), and the relationship is
created.

## Decision — the model

`CoachAthlete.athlete` is a non-null FK, so it can't represent an invite to someone
who isn't a `User` yet. Rather than make the load-bearing relationship model
nullable (which would ripple through every scoping query), the email invite is a
**separate, lightweight pre-relationship artifact**:

- **`CoachInvite(coach, email, token, status)`** — the coach invites an *email*. On
  claim it **materializes** a `CoachAthlete` and immediately activates it (the
  athlete following the claim link *is* their acceptance), then records the
  resulting link + claiming user on itself.

Two "invite" concepts now coexist, by design:

| | `CoachAthlete.invite` (Phase 1 spine) | `CoachInvite` (N4) |
|---|---|---|
| invitee | an existing `User` | an **email** (may have no account) |
| authorization | the recipient `User` is logged in | **bearer token** |
| result | a `pending` link awaiting accept | a claim → **active** link |

### Why bearer-token, not email-match

The claim link is a 122-bit secret delivered to the invited inbox. We do **not**
require the claiming user's email to equal the invited email, because email-only
login coexists with social providers (Google/Facebook) — a new athlete may sign up
with a different address than the coach typed. Whoever controls the invited inbox
and is authenticated may claim. The coach sees who accepted (name/email) on the
roster and can `end` the link if it's wrong.

### Why this rides on allauth with no custom adapter

`ACCOUNT_EMAIL_VERIFICATION` is unset → allauth's default `"optional"`, so signup
logs the user in immediately. The claim view is `@login_required`; an anonymous
visitor is bounced to `/accounts/login/?next=<claim_url>`, and allauth carries
`next` through **both** login and the signup link, returning the now-authenticated
user to the claim page. No custom signup form or adapter needed.

## Phase 1 scope (this PR) — the complete minimal loop

Coach sends → email → athlete claims → active link, end to end:

1. **`CoachInvite` model + migration** — `coach` FK, normalized `email`, unique
   `token`, `status` (`pending`/`accepted`/`declined`/`revoked`), `accepted_by` +
   `accepted_link` FKs (set on claim), timestamps. Partial-unique `(coach, email)`
   while `pending` so re-inviting the same email reuses the open row. State machine:
   `open_for(coach, email)`, `accept(user)` (materializes + activates a
   `CoachAthlete`, idempotent against an already-active link, rejects the coach
   claiming their own invite), `decline()`, `revoke()`. Scoped `CoachInviteQuerySet`
   (`for_coach`, `pending`).
2. **Invite email** — `notifications.emails.send_coach_invite_email(coach, email,
   accept_url)` mirroring `send_week_delivered_email`, + subject/`.md`/`.html`
   templates.
3. **Coach send view** — `POST /meso/invite/` (`coach_invite`): validates the email,
   rejects self-invite, opens/reuses the pending invite, sends the email on
   `transaction.on_commit` (best-effort, never 500s the request), flashes, back to
   roster.
4. **Coach revoke view** — `POST /meso/invite/<token>/revoke/`
   (`coach_invite_revoke`): coach-scoped, pending-only, → roster.
5. **Claim view** — `GET/POST /meso/claim/<token>/` (`invite_claim`):
   `@login_required` (anon → login/signup with `next`); GET renders a confirm page
   (coach + accept/decline); POST `action=accept` materializes the active link and
   lands on `/meso/me/`, `action=decline` marks declined. Already-answered invites
   render/flash a friendly closed state.
6. **Roster surface** — an "Invite an athlete" disclosure (email form) + a "Pending
   invites" list with a Revoke button per row.
7. **Admin, factory, seed** — register `CoachInvite`; `CoachInviteFactory`; the demo
   coach gets one pending invite so the roster surface is visible.

Built **red→green** with a new `test_invites.py` (model state machine, the four
views with auth/scoping/validation, the email send/skip + on-commit best-effort,
the roster surface) plus an email-helper test.

## Phase 2 (built) — close the bidirectional invite loop

The reverse direction the relationship spine always supported in the model
(`CoachAthlete.request` → `pending_athlete_request`) but never in the UI: an
athlete who already has an account asks to train under a coach, the coach
accepts/declines on their roster, and either party sees the pending state on
their own surface. **No migration** — the state machine + the recipient token
views (`invite_accept`/`invite_decline`) already existed; Phase 2 adds the
*initiator* side and the surfaces.

1. **`CoachAthlete.initiator()`** — the mirror of `recipient()`: who opened a
   pending link (coach for an invite, athlete for a request). The initiator is
   who may *withdraw* a pending link, as the recipient is who accepts/declines.
2. **Athlete request view** — `POST /meso/request/` (`athlete_request_coach`):
   resolves the posted email to a *coach* (a `User` with a `CoachProfile`,
   excluding self), rejecting an unknown/non-coach/own address. An already-active
   link is left untouched; an already-pending request (or a coach-invite already
   awaiting the athlete) is a friendly no-op; otherwise `request()` opens (or
   reopens a closed) pending link. The coach is emailed on
   `transaction.on_commit`, best-effort.
3. **Withdraw view** — `POST /meso/request/<token>/withdraw/`
   (`request_withdraw`): initiator-only (the recipient/stranger get 403),
   pending-only; marks the link declined.
4. **Coach response** rides the existing `invite_accept`/`invite_decline`
   recipient views unchanged — a request's recipient *is* the coach.
5. **Request email** — `notifications.send_coach_request_email(athlete, coach,
   roster_url)` + subject/`.md`/`.html` templates, mirroring the invite email;
   skips a coach with no address.
6. **Surfaces** — the coach roster gains a pending-request list (Accept/Decline
   per row); the athlete home gains a "Your coaches" card: incoming invites
   (Accept/Decline), sent requests (Pending + Withdraw), and a request-a-coach
   form.
7. **Routing** — `RosterView` now sends *any* non-coach to `/meso/me/` (a coach =
   has a `CoachProfile`, a coach-side link, or a sent invite), so a brand-new
   athlete or one merely awaiting an invite reaches the request form / pending
   surface instead of an empty coach roster.
8. **Seed** — a seeded pending athlete→coach request (`hopeful@example.com`) so
   the roster's request surface shows on a fresh DB; idempotent + torn down.

Built **red→green** with a new `test_requests.py` (the `initiator()` mirror, the
email helper, the request + withdraw views with auth/scoping/validation, the
coach-response recipient path, both pending surfaces, and the routing) + seed
coverage. **Codex review loop: CLEAN on iteration 1.**

## Deferred (Phase 3+)

- **Resend / expiry** of an invite (today re-inviting reuses the pending row and
  re-sends; no TTL).
- **Stub-athlete** pre-creation (we never create a placeholder `User`).
- **Coach/athlete attribution beyond `accepted_by`**; richer invite history.
- **Coach-side roster filtering by relationship state** beyond pending (e.g. a
  declined/ended history view).
