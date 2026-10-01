"""Review round 1 fixes (#649 #651 #646): activation hooks, locked accept, CRLF, routing."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.urls import reverse

from store_project.meso.billing import activation
from store_project.meso.billing import webhooks as billing_webhooks
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

ACTIVE = CoachAthlete.Status.ACTIVE
WAITING = CoachAthlete.Status.ACCEPTED_WAITING
ENDED = CoachAthlete.Status.ENDED
User = get_user_model()


def make_coach(name="Coach Carter"):
    coach = UserFactory(name=name)
    CoachProfileFactory(user=coach)
    return coach


# -- 1. receiver schedules unconditionally ----------------------------------


class TestActivationReceiver:
    def test_active_save_schedules_callback_even_with_no_waiting_links(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        with mock.patch.object(activation, "_activate_after_commit") as hook:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                CoachSubscriptionFactory(
                    coach=coach, status=CoachSubscription.Status.ACTIVE
                )
        assert len(callbacks) == 1
        hook.assert_called_once_with(coach.pk)

    def test_callback_is_a_noop_without_waiting_links(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        with django_capture_on_commit_callbacks(execute=True):
            CoachSubscriptionFactory(
                coach=coach, status=CoachSubscription.Status.ACTIVE
            )
        assert not CoachAthlete.objects.filter(coach=coach).exists()


# -- 2. invoice.paid recovery activates waiting links ----------------------


class TestInvoicePaidActivates:
    def test_past_due_recovery_activates_waiting_link(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        coach.stripe_customer_id = "cus_r1"
        coach.save(update_fields=["stripe_customer_id"])
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_r1",
        )
        link = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)
        event = {
            "type": "invoice.paid",
            "data": {"object": {"customer": "cus_r1", "subscription": "sub_r1"}},
        }
        with django_capture_on_commit_callbacks(execute=True):
            billing_webhooks.handle_event(event)
        assert (
            CoachSubscription.objects.get(coach=coach).status
            == CoachSubscription.Status.ACTIVE
        )
        link.refresh_from_db()
        assert link.status == ACTIVE


# -- 3. invite_accept locks -------------------------------------------------


def accept_url(link):
    return reverse("meso:invite_accept", kwargs={"token": link.token})


class TestInviteAcceptLocking:
    def test_locks_users_then_link_and_second_accept_is_403(self, client):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        client.force_login(athlete)
        calls = []
        real = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            calls.append((self.model, kwargs))
            return real(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            resp = client.post(accept_url(link))
        assert resp.status_code == 302
        models = [m for m, _ in calls]
        assert User in models and CoachAthlete in models
        assert models.index(User) < models.index(CoachAthlete)
        assert all(kw.get("no_key") for _, kw in calls)
        link.refresh_from_db()
        assert link.status == ACTIVE

        assert client.post(accept_url(link)).status_code == 403

    def test_stranger_is_403(self, client):
        link = CoachAthleteFactory(
            coach=make_coach(),
            athlete=UserFactory(),
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        client.force_login(UserFactory())
        assert client.post(accept_url(link)).status_code == 403


# -- 4. restore messages ----------------------------------------------------


def restore_url(link):
    return reverse("meso:relationship_restore", kwargs={"token": link.token})


def _ended_link(coach, with_plan):
    link = CoachAthleteFactory(coach=coach, athlete=UserFactory(name="Jordan Ellis"))
    if with_plan:
        PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
    client_link = CoachAthlete.objects.get(pk=link.pk)
    client_link.end(by="coach")
    return client_link


class TestRestoreMessages:
    def test_already_active_is_info_not_error(self, client):
        coach = make_coach()
        link = _ended_link(coach, with_plan=True)
        client.force_login(coach)
        client.post(restore_url(link))
        resp = client.post(restore_url(link), follow=True)
        assert resp.redirect_chain[-1][0] == reverse("meso:roster")
        msgs = [(m.level_tag, m.message) for m in resp.context["messages"]]
        assert ("info", "Already restored.") in msgs
        assert not any("can't be restored" in m for _, m in msgs)

    def test_no_plans_back_omits_program_sentence(self, client):
        coach = make_coach()
        link = _ended_link(coach, with_plan=False)
        client.force_login(coach)
        resp = client.post(restore_url(link), follow=True)
        msgs = [m.message for m in resp.context["messages"]]
        assert "Restored Jordan Ellis." in msgs
        assert not any("program is back" in m for m in msgs)


# -- 5. CRLF ----------------------------------------------------------------


class TestCrlf:
    def _setup(self, client):
        link = CoachAthleteFactory(coach=make_coach(), athlete=UserFactory())
        client.force_login(link.coach)
        return link, reverse("meso:athlete_record", kwargs={"pk": link.athlete_id})

    def test_crlf_note_within_browser_limit_saves(self, client):
        link, url = self._setup(client)
        # Interior newlines (the form strips trailing ones before max_length).
        goals = "a" * 750 + "\r\n" * 400 + "a" * 750  # browser 1,900; CRLF 2,300
        client.post(url, {"goals": goals})
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "a" * 750 + "\n" * 400 + "a" * 750
        notes = "x" * 2000 + "\r\n" * 1000 + "x" * 2000  # browser 5,000; CRLF 6,000
        client.post(url, {"notes": notes})
        profile.refresh_from_db()
        assert profile.notes == "x" * 2000 + "\n" * 1000 + "x" * 2000

    def test_label_and_contraindication_crlf_normalised(self, client):
        link, url = self._setup(client)
        client.post(
            url, {"label": "Jo\r\nBlogs", "new_contraindication": "Knee\r\npain"}
        )
        link.refresh_from_db()
        assert "\r" not in link.label
        assert link.athlete.contraindications.filter(text__contains="\r").count() == 0

    def test_genuinely_too_long_still_rejects(self, client):
        link, url = self._setup(client)
        client.post(url, {"goals": "seed"})
        client.post(url, {"goals": "a" * 2001})
        client.post(
            url, {"goals": "b" * 1000 + "\r\n" * 20 + "b" * 981}
        )  # browser: 2,001
        assert AthleteProfile.objects.get(user=link.athlete).goals == "seed"


# -- 7. login routing -------------------------------------------------------


def test_login_redirect_for_waiting_only_athlete(client):
    athlete = UserFactory()
    CoachAthlete.objects.create(coach=UserFactory(), athlete=athlete, status=WAITING)
    resp = client.post(
        reverse("account_login"), {"login": athlete.email, "password": "testpass123"}
    )
    assert resp.status_code == 302
    assert resp.url == "/meso/me/"


# -- 8. has_history ---------------------------------------------------------


class TestHasHistory:
    def _body(self, client, coach):
        client.force_login(coach)
        return client.get(reverse("meso:roster")).content.decode()

    @pytest.mark.parametrize(
        "status",
        [
            CoachAthlete.Status.DECLINED,
            CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
            CoachAthlete.Status.PENDING_COACH_INVITE,
        ],
    )
    def test_declined_or_pending_still_first_run(self, client, status):
        coach = make_coach()
        CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=status)
        assert "Three steps" in self._body(client, coach)

    @pytest.mark.parametrize("status", [ENDED, WAITING])
    def test_ended_or_waiting_hides_first_run(self, client, status):
        coach = make_coach()
        CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=status)
        assert "Three steps" not in self._body(client, coach)


assert CoachProfile  # keep import used
