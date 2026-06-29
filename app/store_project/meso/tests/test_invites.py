"""N4 — athlete onboarding / email invites (Phase 1).

A coach invites a real person *by email* (who may not have an account yet), we
send a tokened claim link, and whoever follows it while authenticated
materializes — and immediately activates — a ``CoachAthlete`` link. This is the
``CoachInvite`` artifact that sits in front of the Phase-1 peer-invite state
machine; see ``docs/meso/invites-plan.md``.

These tests cover:

- the ``CoachInvite`` model state machine (``open_for`` dedup + email
  normalization; ``accept`` materializing/activating a link, idempotent against
  an already-active one, rejecting the coach claiming their own invite;
  ``decline`` / ``revoke``; closed invites don't block a fresh one);
- the invite email helper (sends to the address, names the coach + claim URL;
  skips an empty address);
- the coach send view (login + POST only, validation, self-invite guard, reuse
  of a pending row, best-effort on-commit mail that never 500s a request);
- the coach revoke view (coach-scoped, pending-only);
- the claim view (anon → login with ``next``; GET confirm; POST accept → active
  link + athlete home; POST decline; coach-self guard; already-answered is no
  crash; unknown token 404);
- the roster pending-invite surface.

Mail is deferred to ``transaction.on_commit`` (the view runs under
``ATOMIC_REQUESTS``), so send-asserting tests wrap the request in
``django_capture_on_commit_callbacks(execute=True)`` — the same idiom as
``test_delivery_notifications``.
"""

import uuid
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# -- model -----------------------------------------------------------------


class TestCoachInviteModel:
    def test_open_for_creates_pending(self):
        coach = UserFactory()
        invite, created = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert created is True
        assert invite.status == CoachInvite.Status.PENDING
        assert invite.email == "ath@example.com"
        assert invite.token

    def test_open_for_normalizes_email(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="  Ath@Example.COM ")
        assert invite.email == "ath@example.com"

    def test_open_for_reuses_pending_row(self):
        coach = UserFactory()
        first, c1 = CoachInvite.open_for(coach=coach, email="ath@example.com")
        second, c2 = CoachInvite.open_for(coach=coach, email="ATH@example.com")
        assert c1 is True and c2 is False
        assert first.pk == second.pk
        assert CoachInvite.objects.filter(coach=coach).count() == 1

    def test_accept_materializes_active_link(self):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        link = invite.accept(athlete)
        assert link.coach == coach and link.athlete == athlete
        assert link.is_active
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert invite.accepted_by == athlete
        assert invite.accepted_link == link
        assert invite.responded_at is not None

    def test_accept_idempotent_against_active_link(self):
        coach = UserFactory()
        athlete = UserFactory()
        existing = CoachAthleteFactory(coach=coach, athlete=athlete)  # already active
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        link = invite.accept(athlete)
        assert link.pk == existing.pk
        assert link.is_active
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).count() == 1

    def test_accept_resolves_pending_peer_link(self):
        coach = UserFactory()
        athlete = UserFactory()
        peer = CoachAthlete.invite(coach=coach, athlete=athlete)  # pending
        assert peer.is_pending
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        link = invite.accept(athlete)
        assert link.pk == peer.pk
        assert link.is_active

    def test_coach_cannot_accept_own_invite(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=coach.email)
        with pytest.raises(InvalidTransition):
            invite.accept(coach)
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING

    def test_accept_non_pending_raises(self):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        invite.accept(athlete)
        with pytest.raises(InvalidTransition):
            invite.accept(athlete)

    def test_decline(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.decline()
        assert invite.status == CoachInvite.Status.DECLINED
        assert invite.responded_at is not None

    def test_revoke(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.revoke()
        assert invite.status == CoachInvite.Status.REVOKED
        assert invite.responded_at is not None

    def test_decline_non_pending_raises(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.revoke()
        with pytest.raises(InvalidTransition):
            invite.decline()

    def test_closed_invite_does_not_block_new_one(self):
        coach = UserFactory()
        first, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        first.revoke()
        second, created = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert created is True
        assert second.pk != first.pk
        assert second.status == CoachInvite.Status.PENDING

    def test_queryset_for_coach_pending(self):
        coach = UserFactory()
        other = UserFactory()
        mine, _ = CoachInvite.open_for(coach=coach, email="a@example.com")
        done, _ = CoachInvite.open_for(coach=coach, email="b@example.com")
        done.revoke()
        CoachInvite.open_for(coach=other, email="c@example.com")
        pending = CoachInvite.objects.for_coach(coach).pending()
        assert list(pending) == [mine]


# -- email helper ----------------------------------------------------------


class TestInviteEmail:
    def test_sends_to_address_with_coach_and_url(self):
        from store_project.notifications.emails import send_coach_invite_email

        coach = UserFactory(name="Coach Carter")
        sent = send_coach_invite_email(
            coach=coach,
            email="ath@example.com",
            accept_url="https://x.test/meso/claim/abc/",
        )
        assert sent is True
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["ath@example.com"]
        assert "Coach Carter" in msg.subject + msg.body
        assert "https://x.test/meso/claim/abc/" in msg.body

    def test_skips_empty_address(self):
        from store_project.notifications.emails import send_coach_invite_email

        sent = send_coach_invite_email(
            coach=UserFactory(), email="", accept_url="https://x.test/c/"
        )
        assert sent is False
        assert mail.outbox == []


# -- coach send view -------------------------------------------------------


class TestCoachInviteView:
    url = None

    def setup_method(self):
        self.url = reverse("meso:coach_invite")

    def test_requires_login(self, client):
        resp = client.post(self.url, {"email": "ath@example.com"})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        client.force_login(UserFactory())
        resp = client.get(self.url)
        assert resp.status_code == 405

    def test_valid_email_creates_invite_and_sends(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self.url, {"email": "Ath@Example.com"})
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        invite = CoachInvite.objects.get(coach=coach)
        assert invite.email == "ath@example.com"
        assert invite.status == CoachInvite.Status.PENDING
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ath@example.com"]

    def test_invalid_email_creates_nothing(self, client):
        coach = UserFactory()
        client.force_login(coach)
        resp = client.post(self.url, {"email": "not-an-email"})
        assert resp.status_code == 302
        assert not CoachInvite.objects.filter(coach=coach).exists()
        assert mail.outbox == []

    def test_self_invite_rejected(self, client):
        coach = UserFactory(email="coach@example.com")
        client.force_login(coach)
        resp = client.post(self.url, {"email": "Coach@example.com"})
        assert resp.status_code == 302
        assert not CoachInvite.objects.filter(coach=coach).exists()

    def test_reinvite_reuses_pending_row(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(self.url, {"email": "ath@example.com"})
        with django_capture_on_commit_callbacks(execute=True):
            client.post(self.url, {"email": "ath@example.com"})
        assert CoachInvite.objects.filter(coach=coach).count() == 1
        assert len(mail.outbox) == 2  # re-sent

    def test_mail_failure_does_not_500(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        client.force_login(coach)
        with mock.patch(
            "store_project.meso.views.send_coach_invite_email",
            side_effect=RuntimeError("smtp down"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(self.url, {"email": "ath@example.com"})
        assert resp.status_code == 302
        assert CoachInvite.objects.filter(coach=coach).exists()  # still created


# -- coach revoke view -----------------------------------------------------


class TestCoachInviteRevokeView:
    def test_coach_revokes_pending(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        client.force_login(coach)
        resp = client.post(
            reverse("meso:coach_invite_revoke", kwargs={"token": invite.token})
        )
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.REVOKED

    def test_other_coach_cannot_revoke(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        client.force_login(UserFactory())  # not the inviting coach
        resp = client.post(
            reverse("meso:coach_invite_revoke", kwargs={"token": invite.token})
        )
        assert resp.status_code == 404
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING

    def test_requires_login(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        resp = client.post(
            reverse("meso:coach_invite_revoke", kwargs={"token": invite.token})
        )
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url


# -- claim view ------------------------------------------------------------


class TestInviteClaimView:
    def _url(self, token):
        return reverse("meso:invite_claim", kwargs={"token": token})

    def test_anonymous_redirects_to_login_with_next(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        resp = client.get(self._url(invite.token))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url
        assert self._url(invite.token) in resp.url  # carries ?next=

    def test_authenticated_get_renders_confirm(self, client):
        coach = UserFactory(name="Coach Carter")
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        client.force_login(athlete)
        resp = client.get(self._url(invite.token))
        assert resp.status_code == 200
        assert b"Coach Carter" in resp.content

    def test_accept_creates_active_link(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        client.force_login(athlete)
        resp = client.post(self._url(invite.token), {"action": "accept"})
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.is_active
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert invite.accepted_by == athlete

    def test_decline_marks_declined(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        client.force_login(athlete)
        resp = client.post(self._url(invite.token), {"action": "decline"})
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.DECLINED
        assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()

    def test_coach_self_claim_does_not_link(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=coach.email)
        client.force_login(coach)
        resp = client.post(self._url(invite.token), {"action": "accept"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(coach=coach, athlete=coach).exists()
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING

    def test_already_answered_invite_no_crash(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        invite.revoke()
        client.force_login(athlete)
        resp = client.post(self._url(invite.token), {"action": "accept"})
        assert resp.status_code in (302, 200)
        assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()

    def test_unknown_token_404(self, client):
        client.force_login(UserFactory())
        resp = client.get(self._url(uuid.uuid4()))
        assert resp.status_code == 404


# -- roster surface --------------------------------------------------------


class TestRosterPendingInvites:
    def test_roster_shows_pending_invite(self, client):
        coach = UserFactory()
        CoachInvite.open_for(coach=coach, email="newbie@example.com")
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert b"newbie@example.com" in resp.content

    def test_roster_hides_revoked_invite(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        invite.revoke()
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert b"gone@example.com" not in resp.content
