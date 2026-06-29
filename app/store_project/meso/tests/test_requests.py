"""N4 Phase 2 — closing the bidirectional invite loop.

Phase 1 gave a coach an email-onboarding flow (``CoachInvite``). Phase 2 adds
the *other* direction the relationship spine always supported in the model but
never in the UI: an athlete asks to train under a coach
(``CoachAthlete.request`` → ``pending_athlete_request``), the coach sees the
request on their roster and accepts/declines it (the recipient side, which the
existing ``invite_accept`` / ``invite_decline`` token views already handle), and
either party sees the pending state on their own home. The athlete can also
**withdraw** a request they sent (the initiator side).

These tests cover:

- the ``CoachAthlete.initiator()`` mirror of ``recipient()``;
- the coach-request email helper (sends to the coach, names the athlete + URL;
  skips a coach with no address);
- the athlete request view (login + POST only; coach-by-email lookup scoped to
  real coaches; unknown/non-coach/self rejected; active + already-pending +
  reopen-a-closed-link handling; best-effort on-commit mail that never 500s);
- the withdraw view (initiator-only; recipient/stranger forbidden; pending-only);
- the coach responding to a request via the existing recipient views;
- the athlete-home pending surface (invites awaiting me + requests I sent + the
  request form);
- the coach roster pending-request surface;
- the roster routing that now sends any non-coach to their training home so a
  brand-new athlete can reach the request form.
"""

from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import CoachAthlete
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_coach(email="coach@example.com", name="Coach Carter"):
    """A User who *is* a coach — a ``CoachProfile`` is what the lookup keys on."""
    coach = UserFactory(email=email, name=name)
    CoachProfileFactory(user=coach)
    return coach


# -- model -----------------------------------------------------------------


class TestInitiator:
    def test_initiator_of_a_request_is_the_athlete(self):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        assert link.initiator() == athlete

    def test_initiator_of_an_invite_is_the_coach(self):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        assert link.initiator() == coach

    def test_initiator_none_when_not_pending(self):
        link = CoachAthleteFactory()  # active
        assert link.initiator() is None


# -- email helper ----------------------------------------------------------


class TestCoachRequestEmail:
    def test_sends_to_coach_with_athlete_and_url(self):
        from store_project.notifications.emails import send_coach_request_email

        coach = make_coach(email="coach@example.com")
        athlete = UserFactory(name="Ana Athlete")
        sent = send_coach_request_email(
            athlete=athlete,
            coach=coach,
            roster_url="https://x.test/meso/",
        )
        assert sent is True
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["coach@example.com"]
        assert "Ana Athlete" in msg.subject + msg.body
        assert "https://x.test/meso/" in msg.body

    def test_skips_coach_with_no_address(self):
        from store_project.notifications.emails import send_coach_request_email

        coach = UserFactory(email="")
        sent = send_coach_request_email(
            athlete=UserFactory(), coach=coach, roster_url="https://x.test/meso/"
        )
        assert sent is False
        assert mail.outbox == []


# -- athlete request view --------------------------------------------------


class TestAthleteRequestCoachView:
    url = None

    def setup_method(self):
        self.url = reverse("meso:athlete_request_coach")

    def test_requires_login(self, client):
        resp = client.post(self.url, {"email": "coach@example.com"})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        client.force_login(UserFactory())
        resp = client.get(self.url)
        assert resp.status_code == 405

    def test_valid_coach_creates_request_and_emails(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach(email="coach@example.com")
        athlete = UserFactory()
        client.force_login(athlete)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self.url, {"email": "Coach@Example.com"})
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert link.invited_by == CoachAthlete.InvitedBy.ATHLETE
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["coach@example.com"]

    def test_unknown_email_creates_nothing(self, client):
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.post(self.url, {"email": "nobody@example.com"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()
        assert mail.outbox == []

    def test_non_coach_email_creates_nothing(self, client):
        # A real user, but not a coach (no CoachProfile) — not a valid target.
        plain = UserFactory(email="plain@example.com")
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.post(self.url, {"email": "plain@example.com"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(coach=plain, athlete=athlete).exists()

    def test_invalid_email_creates_nothing(self, client):
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.post(self.url, {"email": "not-an-email"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()

    def test_self_request_rejected(self, client):
        # A coach who posts their own email can't request themselves.
        coach = make_coach(email="solo@example.com")
        client.force_login(coach)
        resp = client.post(self.url, {"email": "solo@example.com"})
        assert resp.status_code == 302
        assert not CoachAthlete.objects.filter(coach=coach, athlete=coach).exists()

    def test_already_active_link_makes_no_pending(self, client):
        coach = make_coach()
        athlete = UserFactory()
        CoachAthleteFactory(coach=coach, athlete=athlete)  # active
        client.force_login(athlete)
        resp = client.post(self.url, {"email": coach.email})
        assert resp.status_code == 302
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.is_active  # unchanged, not flipped to pending
        assert mail.outbox == []

    def test_duplicate_request_is_idempotent(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        athlete = UserFactory()
        client.force_login(athlete)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(self.url, {"email": coach.email})
        client.post(self.url, {"email": coach.email})  # second time
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).count() == 1

    def test_reopens_a_previously_declined_link(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        link.decline()  # closed
        client.force_login(athlete)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(self.url, {"email": coach.email})
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST

    def test_mail_failure_does_not_500(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        athlete = UserFactory()
        client.force_login(athlete)
        with mock.patch(
            "store_project.meso.views.send_coach_request_email",
            side_effect=RuntimeError("smtp down"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(self.url, {"email": coach.email})
        assert resp.status_code == 302
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()


# -- withdraw view ---------------------------------------------------------


class TestRequestWithdrawView:
    def _url(self, token):
        return reverse("meso:request_withdraw", kwargs={"token": token})

    def test_initiator_withdraws_pending_request(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(athlete)
        resp = client.post(self._url(link.token))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED

    def test_recipient_cannot_withdraw(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(coach)  # the recipient, not the initiator
        resp = client.post(self._url(link.token))
        assert resp.status_code == 403
        link.refresh_from_db()
        assert link.is_pending

    def test_stranger_cannot_withdraw(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(UserFactory())
        resp = client.post(self._url(link.token))
        assert resp.status_code == 403

    def test_cannot_withdraw_active_link(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthleteFactory(coach=coach, athlete=athlete)  # active
        client.force_login(athlete)
        resp = client.post(self._url(link.token))
        assert resp.status_code == 403

    def test_requires_login(self, client):
        link = CoachAthlete.request(athlete=UserFactory(), coach=UserFactory())
        resp = client.post(self._url(link.token))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url


# -- coach responds to a request (recipient side) --------------------------


class TestCoachRespondsToRequest:
    def test_coach_accepts_request(self, client):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.is_active

    def test_coach_declines_request(self, client):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:invite_decline", kwargs={"token": link.token}))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED

    def test_athlete_cannot_accept_own_request(self, client):
        # The athlete is the initiator, not the recipient — they can't self-accept.
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(athlete)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 403
        link.refresh_from_db()
        assert link.is_pending


# -- athlete home pending surface ------------------------------------------


class TestAthleteHomePending:
    home = None

    def setup_method(self):
        self.home = reverse("meso:athlete_home")

    def test_home_shows_request_form(self, client):
        client.force_login(UserFactory())
        body = client.get(self.home).content.decode()
        assert reverse("meso:athlete_request_coach") in body

    def test_home_shows_sent_request_with_withdraw(self, client):
        coach = make_coach(name="Coach Carter")
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(athlete)
        body = client.get(self.home).content.decode()
        assert "Coach Carter" in body
        assert reverse("meso:request_withdraw", kwargs={"token": link.token}) in body

    def test_home_shows_incoming_invite_with_accept_decline(self, client):
        coach = make_coach(name="Coach Carter")
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)  # awaiting athlete
        client.force_login(athlete)
        body = client.get(self.home).content.decode()
        assert "Coach Carter" in body
        assert reverse("meso:invite_accept", kwargs={"token": link.token}) in body
        assert reverse("meso:invite_decline", kwargs={"token": link.token}) in body


# -- coach roster pending-request surface ----------------------------------


class TestRosterPendingRequests:
    def test_roster_shows_pending_request(self, client):
        coach = make_coach()
        athlete = UserFactory(name="Ana Athlete")
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Ana Athlete" in body
        assert reverse("meso:invite_accept", kwargs={"token": link.token}) in body
        assert reverse("meso:invite_decline", kwargs={"token": link.token}) in body

    def test_roster_hides_other_coachs_request(self, client):
        coach = make_coach()
        other = make_coach(email="other@example.com")
        CoachAthlete.request(athlete=UserFactory(name="Not Mine"), coach=other)
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Not Mine" not in body

    def test_roster_has_no_request_row_without_requests(self, client):
        coach = make_coach()
        CoachAthleteFactory(coach=coach, athlete=UserFactory())  # active, not a request
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        # No pending requests → the "wants to train with you" affordance is absent.
        assert "wants to train" not in body


# -- roster routing --------------------------------------------------------


class TestRosterRouting:
    def test_brand_new_user_redirected_to_home(self, client):
        # No CoachProfile, no links, no sent invites → treated as an athlete.
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")

    def test_pending_invite_recipient_reaches_home(self, client):
        # An athlete with only a pending coach-invite (no active link) must still
        # reach their home to respond to it.
        coach = make_coach()
        athlete = UserFactory()
        CoachAthlete.invite(coach=coach, athlete=athlete)
        client.force_login(athlete)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")

    def test_coach_with_only_a_sent_invite_stays_on_roster(self, client):
        # A coach identified solely by an email invite they sent keeps the roster.
        from store_project.meso.models import CoachInvite

        coach = UserFactory()
        CoachInvite.open_for(coach=coach, email="prospect@example.com")
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200

    def test_coach_with_a_pending_request_stays_on_roster(self, client):
        # A coach awaiting an athlete's request keeps the roster (they coach).
        coach = UserFactory()
        CoachAthlete.request(athlete=UserFactory(), coach=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
