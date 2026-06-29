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

## Deferred (Phase 2+)

- **Athlete → coach request** UI (the `CoachAthlete.request` half) and a pending
  surface on the athlete home.
- **Resend / expiry** of an invite (today re-inviting reuses the pending row and
  re-sends; no TTL).
- **Stub-athlete** pre-creation (we never create a placeholder `User`).
- **Coach/athlete attribution beyond `accepted_by`**; richer invite history.
