"""N4 — invite lifecycle (Phase 3): expiry / TTL + explicit resend.

The Phase-1 ``CoachInvite`` had no notion of staleness — a tokened claim link
worked forever — and no way to re-arm one beyond re-typing the email. Phase 3
adds a time-to-live and an explicit *resend*:

- every new invite gets an ``expires_at`` (``open_for`` stamps it ``now + TTL``);
- ``is_expired`` / ``is_claimable`` derive claimability from that clock;
- ``expire()`` is the ``pending → expired`` transition (a swept or lazily-aged
  invite), with a new ``Status.EXPIRED``; the claim path refuses an expired token
  and ``accept()`` can never materialize a link from one;
- ``resend()`` re-arms an invite — a **fresh token** (the old emailed link dies)
  and a reset clock — and brings an already-expired one back to pending;
- ``open_for`` re-arms an expired/overdue row instead of orphaning it, so a
  re-invite via the roster form still resolves to one outstanding row;
- the ``meso_expire_invites`` management command sweeps overdue pending invites;
- the coach roster surfaces expired invites with a Resend action.

See ``docs/meso/invites-plan.md``. Mail is best-effort on
``transaction.on_commit``, so send-asserting view tests wrap the request in
``django_capture_on_commit_callbacks`` (same idiom as ``test_invites``).
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _age(invite, *, days):
    """Push an invite's clock ``days`` into the past (negative = future)."""
    invite.expires_at = timezone.now() - timedelta(days=days)
    invite.save(update_fields=["expires_at"])
    return invite


# -- model: expiry clock ---------------------------------------------------


class TestInviteExpiryClock:
    def test_open_for_stamps_future_expiry(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert invite.expires_at is not None
        assert invite.expires_at > timezone.now()

    def test_is_expired_false_for_future(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert invite.is_expired is False
        assert invite.is_claimable is True

    def test_is_expired_true_for_past(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        assert invite.is_expired is True
        assert invite.is_claimable is False

    def test_null_expiry_never_expires(self):
        """A legacy pending invite (no clock) stays claimable — data-safe."""
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.expires_at = None
        invite.save(update_fields=["expires_at"])
        assert invite.is_expired is False
        assert invite.is_claimable is True


# -- model: expire() transition --------------------------------------------


class TestInviteExpireTransition:
    def test_expire_overdue_pending(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        assert invite.status == CoachInvite.Status.EXPIRED

    def test_expire_not_yet_due_raises(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        with pytest.raises(InvalidTransition):
            invite.expire()
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING

    def test_expire_non_pending_raises(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.revoke()
        with pytest.raises(InvalidTransition):
            invite.expire()

    def test_expired_status_is_not_claimable(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        assert invite.is_claimable is False

    def test_revoke_dismisses_expired_invite(self):
        """A coach can clear a dead invite off the roster, not just a live one."""
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        invite.revoke()
        assert invite.status == CoachInvite.Status.REVOKED


# -- model: accept() rejects an expired invite -----------------------------


class TestAcceptRejectsExpired:
    def test_accept_expired_raises_and_marks_expired(self):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        _age(invite, days=1)
        with pytest.raises(InvalidTransition):
            invite.accept(athlete)
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.EXPIRED
        assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()


# -- model: resend() -------------------------------------------------------


class TestInviteResend:
    def test_resend_rotates_token_and_resets_clock(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        old_token = invite.token
        _age(invite, days=1)  # overdue
        invite.resend()
        assert invite.token != old_token  # the old emailed link dies
        assert invite.status == CoachInvite.Status.PENDING
        assert invite.expires_at > timezone.now()
        assert invite.is_claimable is True

    def test_resend_rearms_expired_invite(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        assert invite.status == CoachInvite.Status.EXPIRED
        invite.resend()
        assert invite.status == CoachInvite.Status.PENDING
        assert invite.is_claimable is True

    def test_resend_clears_responded_at(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        invite.resend()
        assert invite.responded_at is None

    def test_resend_persists(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        old_token = invite.token
        invite.resend()
        invite.refresh_from_db()
        assert invite.token != old_token
        assert invite.status == CoachInvite.Status.PENDING

    @pytest.mark.parametrize("closer", ["decline", "revoke"])
    def test_resend_answered_invite_raises(self, closer):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        getattr(invite, closer)()
        with pytest.raises(InvalidTransition):
            invite.resend()

    def test_resend_accepted_invite_raises(self):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        invite.accept(athlete)
        with pytest.raises(InvalidTransition):
            invite.resend()


# -- model: querysets ------------------------------------------------------


class TestLifecycleQuerysets:
    def test_claimable_excludes_expired(self):
        coach = UserFactory()
        live, _ = CoachInvite.open_for(coach=coach, email="live@example.com")
        overdue, _ = CoachInvite.open_for(coach=coach, email="overdue@example.com")
        _age(overdue, days=1)
        gone, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        _age(gone, days=1)
        gone.expire()
        assert set(CoachInvite.objects.claimable()) == {live}

    def test_overdue_only_past_due_pending(self):
        coach = UserFactory()
        live, _ = CoachInvite.open_for(coach=coach, email="live@example.com")
        overdue, _ = CoachInvite.open_for(coach=coach, email="overdue@example.com")
        _age(overdue, days=1)
        gone, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        _age(gone, days=1)
        gone.expire()  # already EXPIRED, not "overdue pending"
        assert set(CoachInvite.objects.overdue()) == {overdue}

    def test_outstanding_includes_pending_and_expired(self):
        coach = UserFactory()
        live, _ = CoachInvite.open_for(coach=coach, email="live@example.com")
        gone, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        _age(gone, days=1)
        gone.expire()
        revoked, _ = CoachInvite.open_for(coach=coach, email="rv@example.com")
        revoked.revoke()
        assert set(CoachInvite.objects.outstanding()) == {live, gone}


# -- model: open_for re-arms a stale row -----------------------------------


class TestOpenForRearm:
    def test_open_for_rearms_expired_row(self):
        coach = UserFactory()
        first, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(first, days=1)
        first.expire()
        second, created = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert created is False
        assert second.pk == first.pk
        assert second.status == CoachInvite.Status.PENDING
        assert second.is_claimable is True
        assert CoachInvite.objects.filter(coach=coach).count() == 1

    def test_open_for_rearms_overdue_pending_row(self):
        coach = UserFactory()
        first, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(first, days=1)  # past due but not yet swept to EXPIRED
        second, created = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert created is False
        assert second.pk == first.pk
        assert second.is_claimable is True

    def test_open_for_live_pending_unchanged(self):
        coach = UserFactory()
        first, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        token = first.token
        second, created = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert created is False
        assert second.pk == first.pk
        assert second.token == token  # a live link is not rotated out from under


# -- sweep command ---------------------------------------------------------


class TestExpireCommand:
    def test_sweeps_overdue_to_expired(self):
        coach = UserFactory()
        live, _ = CoachInvite.open_for(coach=coach, email="live@example.com")
        overdue, _ = CoachInvite.open_for(coach=coach, email="overdue@example.com")
        _age(overdue, days=1)
        call_command("meso_expire_invites")
        overdue.refresh_from_db()
        live.refresh_from_db()
        assert overdue.status == CoachInvite.Status.EXPIRED
        assert live.status == CoachInvite.Status.PENDING

    def test_idempotent_when_nothing_overdue(self):
        coach = UserFactory()
        CoachInvite.open_for(coach=coach, email="live@example.com")
        call_command("meso_expire_invites")  # no error, nothing flipped
        assert CoachInvite.objects.claimable().count() == 1


# -- claim view rejects an expired token -----------------------------------


class TestClaimViewExpiry:
    def _url(self, token):
        return reverse("meso:invite_claim", kwargs={"token": token})

    def test_get_expired_marks_expired_and_hides_accept(self, client):
        coach = UserFactory(name="Coach Carter")
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        _age(invite, days=1)
        client.force_login(athlete)
        resp = client.get(self._url(invite.token))
        assert resp.status_code == 200
        assert b"expired" in resp.content.lower()
        assert b'value="accept"' not in resp.content
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.EXPIRED

    def test_post_accept_expired_makes_no_link(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        _age(invite, days=1)
        client.force_login(athlete)
        resp = client.post(self._url(invite.token), {"action": "accept"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.EXPIRED

    def test_old_token_cannot_claim_after_resend(self, client):
        """Resend rotates the token, so a claim via the old link can't accept."""
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        old_token = invite.token
        invite.resend()  # rotates the token; the old link must die
        client.force_login(athlete)
        stale = client.post(self._url(old_token), {"action": "accept"})
        assert stale.status_code == 404
        assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()
        # the freshly rotated token still works
        fresh = client.post(self._url(invite.token), {"action": "accept"})
        assert fresh.status_code == 302
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()

    def test_get_old_token_after_resend_404_no_leak(self, client):
        """A stale GET link can't render the form (nor leak the rotated token)."""
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        old_token = invite.token
        invite.resend()  # rotates the token
        client.force_login(athlete)
        resp = client.get(self._url(old_token))
        assert resp.status_code == 404
        assert str(invite.token).encode() not in resp.content  # new token not leaked


# -- coach resend view -----------------------------------------------------


class TestCoachInviteResendView:
    def _url(self, token):
        return reverse("meso:coach_invite_resend", kwargs={"token": token})

    def test_requires_login(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        resp = client.post(self._url(invite.token))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        client.force_login(coach)
        resp = client.get(self._url(invite.token))
        assert resp.status_code == 405

    def test_coach_resends_pending(self, client, django_capture_on_commit_callbacks):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        old_token = invite.token
        _age(invite, days=1)
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self._url(invite.token))
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.token != old_token
        assert invite.status == CoachInvite.Status.PENDING
        assert invite.is_claimable is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ath@example.com"]

    def test_coach_resends_expired(self, client, django_capture_on_commit_callbacks):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _age(invite, days=1)
        invite.expire()
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self._url(invite.token))
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING
        assert len(mail.outbox) == 1

    def test_other_coach_cannot_resend(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        client.force_login(UserFactory())
        resp = client.post(self._url(invite.token))
        assert resp.status_code == 404
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING

    def test_resend_answered_invite_noops(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.revoke()
        client.force_login(coach)
        resp = client.post(self._url(invite.token))
        assert resp.status_code == 302  # friendly redirect, no 500
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.REVOKED

    def test_mail_failure_does_not_500(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        client.force_login(coach)
        with mock.patch(
            "store_project.meso.views.send_coach_invite_email",
            side_effect=RuntimeError("smtp down"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(self._url(invite.token))
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING  # resend still applied


# -- roster surface --------------------------------------------------------


class TestRosterLifecycleSurface:
    def test_roster_shows_expired_invite_with_resend(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="stale@example.com")
        _age(invite, days=1)
        invite.expire()
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert b"stale@example.com" in resp.content
        resend_url = reverse("meso:coach_invite_resend", kwargs={"token": invite.token})
        assert resend_url.encode() in resp.content

    def test_roster_shows_resend_for_pending(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="newbie@example.com")
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        resend_url = reverse("meso:coach_invite_resend", kwargs={"token": invite.token})
        assert resend_url.encode() in resp.content

    def test_roster_hides_revoked_invite(self, client):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        invite.revoke()
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert b"gone@example.com" not in resp.content
