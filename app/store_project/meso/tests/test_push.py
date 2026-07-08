"""Athlete slice Phase 4b — web push (decisions S3/S7).

The push peer of Phase 4a's delivery email. Three seams:

- **subscribe / unsubscribe** (``/meso/api/me/push/...``): the athlete's browser
  registers its ``PushSubscription`` (endpoint + keys); the row is owned by the
  logged-in athlete, unique per endpoint (re-subscribing upserts), and only the
  owner can drop it.
- **the sender** (``meso.push``): signs + sends an encrypted push per device,
  prunes dead endpoints (404/410 Gone), and is a silent no-op when VAPID keys
  aren't configured — the same graceful degradation as the no-address email.
- **the deliver hook**: a successful deliver pushes to the athlete's devices,
  best-effort (a push failure never rolls back or 500s the deliver) and
  athlete-only (never the coach).

The network send (``pywebpush.webpush``) is always mocked — these tests exercise
our wiring, signing arguments, pruning, and scoping, never a real push service.
The deliver hook is deferred to ``transaction.on_commit``, so the integration
tests wrap the request in ``django_capture_on_commit_callbacks`` (the Phase 4a
idiom).
"""

import json
from unittest import mock

import pytest
from django.test import override_settings
from django.urls import reverse
from pywebpush import WebPushException

from store_project.meso import push as meso_push
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import PushSubscription
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

PUSH_PATH = "store_project.meso.push.webpush"
DISABLED = dict(MESO_VAPID_PUBLIC_KEY="", MESO_VAPID_PRIVATE_KEY="")


def make_sub(athlete, endpoint="https://push.example.com/abc", p256dh="pk", auth="ak"):
    return PushSubscription.objects.create(
        athlete=athlete, endpoint=endpoint, p256dh=p256dh, auth=auth
    )


def gone_exception(status):
    return WebPushException("gone", response=mock.Mock(status_code=status))


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, week


SUBSCRIBE = reverse("meso:push_subscribe")
UNSUBSCRIBE = reverse("meso:push_unsubscribe")


def sub_body(endpoint="https://push.example.com/abc", p256dh="pk", auth="ak"):
    return json.dumps({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}})


# -- model -----------------------------------------------------------------


class TestPushSubscriptionModel:
    def test_subscription_info_shape(self):
        sub = make_sub(UserFactory(), endpoint="https://p/x", p256dh="KEY", auth="AUTH")
        assert sub.as_subscription_info() == {
            "endpoint": "https://p/x",
            "keys": {"p256dh": "KEY", "auth": "AUTH"},
        }


# -- subscribe endpoint ----------------------------------------------------


class TestSubscribe:
    def test_requires_login(self, client):
        resp = client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_requires_post(self, client):
        client.force_login(UserFactory())
        assert client.get(SUBSCRIBE).status_code == 405

    def test_stores_subscription_for_caller(self, client):
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.post(
            SUBSCRIBE,
            data=sub_body(p256dh="PK", auth="AK"),
            content_type="application/json",
        )
        assert resp.status_code == 201
        sub = PushSubscription.objects.get()
        assert sub.athlete == athlete
        assert sub.endpoint == "https://push.example.com/abc"
        assert sub.p256dh == "PK"
        assert sub.auth == "AK"

    def test_endpoint_is_unique_upsert(self, client):
        athlete = UserFactory()
        client.force_login(athlete)
        client.post(
            SUBSCRIBE, data=sub_body(p256dh="OLD"), content_type="application/json"
        )
        client.post(
            SUBSCRIBE, data=sub_body(p256dh="NEW"), content_type="application/json"
        )
        # Same endpoint → one row, refreshed keys (no duplicate device).
        assert PushSubscription.objects.count() == 1
        assert PushSubscription.objects.get().p256dh == "NEW"

    def test_endpoint_reassigns_to_current_athlete(self, client):
        # Same browser/endpoint, different user logs in → the row follows them.
        first = UserFactory()
        make_sub(first, endpoint="https://push.example.com/abc")
        second = UserFactory()
        client.force_login(second)
        client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")
        assert PushSubscription.objects.count() == 1
        assert PushSubscription.objects.get().athlete == second

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            json.dumps([]),
            json.dumps({"keys": {"p256dh": "a", "auth": "b"}}),  # no endpoint
            json.dumps({"endpoint": "https://p/x"}),  # no keys
            json.dumps({"endpoint": "https://p/x", "keys": {"auth": "b"}}),  # no p256dh
            json.dumps({"endpoint": "https://p/x", "keys": {"p256dh": "a"}}),  # no auth
            json.dumps({"endpoint": "x" * 600, "keys": {"p256dh": "a", "auth": "b"}}),
        ],
    )
    def test_bad_body_is_400_and_writes_nothing(self, client, body):
        client.force_login(UserFactory())
        resp = client.post(SUBSCRIBE, data=body, content_type="application/json")
        assert resp.status_code == 400
        assert PushSubscription.objects.count() == 0

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://push.example.com/abc",  # not HTTPS
            "ftp://push.example.com/abc",  # not HTTPS
            "https://localhost/abc",
            "https://127.0.0.1/abc",
            "https://10.0.0.5/abc",  # private
            "https://169.254.169.254/latest/meta-data/",  # cloud metadata (SSRF)
            "https://100.64.0.1/abc",  # CGNAT — non-private but not globally routable
        ],
    )
    def test_unsafe_endpoint_rejected(self, client, endpoint):
        # The stored endpoint is fetched server-side by pywebpush at deliver time,
        # so an internal/non-HTTPS target must be refused before it's persisted.
        client.force_login(UserFactory())
        resp = client.post(
            SUBSCRIBE, data=sub_body(endpoint=endpoint), content_type="application/json"
        )
        assert resp.status_code == 400
        assert PushSubscription.objects.count() == 0


# -- unsubscribe endpoint --------------------------------------------------


class TestUnsubscribe:
    def test_removes_own_subscription(self, client):
        athlete = UserFactory()
        make_sub(athlete, endpoint="https://push.example.com/abc")
        client.force_login(athlete)
        resp = client.post(
            UNSUBSCRIBE,
            data=json.dumps({"endpoint": "https://push.example.com/abc"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert PushSubscription.objects.count() == 0

    def test_cannot_remove_another_athletes_subscription(self, client):
        owner = UserFactory()
        make_sub(owner, endpoint="https://push.example.com/abc")
        client.force_login(UserFactory())  # someone else
        client.post(
            UNSUBSCRIBE,
            data=json.dumps({"endpoint": "https://push.example.com/abc"}),
            content_type="application/json",
        )
        # The owner's subscription is untouched.
        assert PushSubscription.objects.filter(athlete=owner).count() == 1

    def test_unknown_endpoint_is_quiet_success(self, client):
        client.force_login(UserFactory())
        resp = client.post(
            UNSUBSCRIBE,
            data=json.dumps({"endpoint": "https://push.example.com/nope"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_requires_login(self, client):
        resp = client.post(
            UNSUBSCRIBE,
            data=json.dumps({"endpoint": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 302


# -- the sender ------------------------------------------------------------


class TestSendWebPush:
    def test_signs_and_sends(self):
        sub = make_sub(UserFactory())
        with mock.patch(PUSH_PATH) as webpush:
            sent = meso_push.send_web_push(
                sub.as_subscription_info(), {"title": "Hi", "url": "/meso/me/"}
            )
        assert sent is True
        kwargs = webpush.call_args.kwargs
        assert kwargs["subscription_info"] == sub.as_subscription_info()
        assert json.loads(kwargs["data"]) == {"title": "Hi", "url": "/meso/me/"}
        assert kwargs["vapid_private_key"]  # the configured signing key
        assert kwargs["vapid_claims"]["sub"].startswith("mailto:")
        # A bounded network wait so a slow endpoint can't hang the deliver hook.
        assert kwargs["timeout"]

    @override_settings(**DISABLED)
    def test_noop_when_disabled(self):
        sub = make_sub(UserFactory())
        with mock.patch(PUSH_PATH) as webpush:
            sent = meso_push.send_web_push(sub.as_subscription_info(), {"title": "Hi"})
        assert sent is False
        webpush.assert_not_called()


class TestNotifyWeekDelivered:
    def _call(self, athlete, plan, week):
        return meso_push.notify_week_delivered(
            athlete=athlete,
            coach=plan.coach,
            plan=plan,
            week=week,
            home_url="http://testserver/meso/me/",
        )

    def test_pushes_to_each_device_with_payload(self):
        plan, week = seed_plan()
        athlete = plan.athlete
        make_sub(athlete, endpoint="https://push/1")
        make_sub(athlete, endpoint="https://push/2")
        with mock.patch(PUSH_PATH) as webpush:
            sent = self._call(athlete, plan, week)
        assert sent == 2
        assert webpush.call_count == 2
        payload = json.loads(webpush.call_args.kwargs["data"])
        assert plan.coach.display_name() in payload["body"]
        assert plan.title in payload["body"]
        assert "Week 1" in payload["body"]
        assert payload["url"] == "http://testserver/meso/me/"

    def test_prunes_dead_subscription(self):
        plan, week = seed_plan()
        athlete = plan.athlete
        make_sub(athlete, endpoint="https://push/dead")
        with mock.patch(PUSH_PATH, side_effect=gone_exception(410)):
            sent = self._call(athlete, plan, week)
        assert sent == 0
        assert PushSubscription.objects.filter(athlete=athlete).count() == 0

    def test_other_failure_is_swallowed_and_keeps_subscription(self):
        plan, week = seed_plan()
        athlete = plan.athlete
        make_sub(athlete, endpoint="https://push/flaky")
        with mock.patch(PUSH_PATH, side_effect=gone_exception(500)):
            sent = self._call(athlete, plan, week)
        # A transient 500 isn't "gone" — swallow it, keep the subscription.
        assert sent == 0
        assert PushSubscription.objects.filter(athlete=athlete).count() == 1

    def test_no_subscriptions_is_noop(self):
        plan, week = seed_plan()
        with mock.patch(PUSH_PATH) as webpush:
            sent = self._call(plan.athlete, plan, week)
        assert sent == 0
        webpush.assert_not_called()

    @override_settings(**DISABLED)
    def test_disabled_is_noop(self):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/1")
        with mock.patch(PUSH_PATH) as webpush:
            sent = self._call(plan.athlete, plan, week)
        assert sent == 0
        webpush.assert_not_called()

    def test_only_the_athletes_subscriptions(self):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/athlete")
        make_sub(plan.coach, endpoint="https://push/coach")  # must be ignored
        with mock.patch(PUSH_PATH) as webpush:
            sent = self._call(plan.athlete, plan, week)
        assert sent == 1
        endpoints = {
            c.kwargs["subscription_info"]["endpoint"] for c in webpush.call_args_list
        }
        assert endpoints == {"https://push/athlete"}


# -- the deliver hook ------------------------------------------------------


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


class TestDeliverTriggersPush:
    def test_deliver_pushes_to_the_athlete(
        self, client, django_capture_on_commit_callbacks
    ):
        plan, _ = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/athlete")
        client.force_login(plan.coach)
        with mock.patch(PUSH_PATH) as webpush:
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(deliver_url(plan))
        assert resp.status_code == 201
        assert webpush.call_count == 1
        assert (
            webpush.call_args.kwargs["subscription_info"]["endpoint"]
            == "https://push/athlete"
        )

    def test_coach_is_not_pushed(self, client, django_capture_on_commit_callbacks):
        plan, _ = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/athlete")
        make_sub(plan.coach, endpoint="https://push/coach")
        client.force_login(plan.coach)
        with mock.patch(PUSH_PATH) as webpush:
            with django_capture_on_commit_callbacks(execute=True):
                client.post(deliver_url(plan))
        endpoints = {
            c.kwargs["subscription_info"]["endpoint"] for c in webpush.call_args_list
        }
        assert endpoints == {"https://push/athlete"}

    def test_push_failure_does_not_break_delivery(
        self, client, django_capture_on_commit_callbacks
    ):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/athlete")
        client.force_login(plan.coach)
        with mock.patch(PUSH_PATH, side_effect=RuntimeError("push service down")):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(deliver_url(plan))
        # The push blew up inside the on_commit callback; the deliver still committed.
        assert resp.status_code == 201
        week.refresh_from_db()
        assert week.delivered_at is not None

    def test_forbidden_deliver_pushes_nothing(self, client):
        plan, _ = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/athlete")
        client.force_login(UserFactory())  # a stranger
        with mock.patch(PUSH_PATH) as webpush:
            resp = client.post(deliver_url(plan))
        assert resp.status_code == 403
        webpush.assert_not_called()


# -- push config exposed to the page ---------------------------------------


class TestPushConfigInPage:
    def _seed_athlete(self):
        from django.utils import timezone

        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        meso = MesocycleFactory(plan=plan, order=0)
        week = WeekFactory(
            mesocycle=meso, index=1, is_current=True, delivered_at=timezone.now()
        )
        day(week, day_number=1, name="Lower")
        return rel.athlete

    def test_home_exposes_vapid_key_and_subscribe_url(self, client):
        client.force_login(self._seed_athlete())
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert 'name="meso-vapid-key"' in body
        assert meso_push.vapid_public_key() in body
        assert reverse("meso:push_subscribe") in body
        assert 'data-push-enabled="1"' in body

    @override_settings(**DISABLED)
    def test_disabled_hides_key_but_page_still_works(self, client):
        client.force_login(self._seed_athlete())
        resp = client.get(reverse("meso:athlete_home"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="meso-vapid-key"' not in body
        assert 'data-push-enabled="0"' in body
        # The PWA itself is unaffected — manifest + worker still wired.
        assert reverse("meso:manifest") in body
