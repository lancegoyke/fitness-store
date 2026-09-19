"""``analytics.views.track_beacon`` and ``analytics.beacon.validate`` (#509 slice 3).

Two things are pinned here:

- ``beacon.validate`` is the closed-set policy itself — pure, no request or
  database involved — so its rejection order and messages are tested
  directly, independent of the view around it.
- ``track_beacon`` is the HTTP surface: auth, body size, JSON shape, rate
  limiting, and that a valid post actually reaches ``Event`` with
  ``source="client"``. The sandbox/staff exclusion is deliberately tested
  only through the view (asserting the 204 + no row), not by asserting
  ``track()`` was called with particular arguments, because that exclusion
  is ``track()``'s own contract (see ``test_track.py``) — this file only
  needs to know the beacon doesn't route around it.
"""

import json

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from store_project.analytics import beacon
from store_project.analytics.models import Event
from store_project.meso import sandbox
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Isolate the per-user beacon rate counter between tests.

    Same reasoning as ``meso.tests.test_sandbox``'s fixture of the same
    name: LocMemCache persists per-process, so an earlier test's counter
    could otherwise bleed into a later one — unlikely here since the key is
    per user pk and factories mint a fresh user per test, but a stray
    collision would be a confusing failure to chase down, and clearing is
    free.
    """
    cache.clear()
    yield
    cache.clear()


def _url():
    return reverse("meso:track_beacon")


def _post(client, name, props=None, **extra):
    body = {"name": name}
    if props is not None:
        body["props"] = props
    return client.post(
        _url(), data=json.dumps(body), content_type="application/json", **extra
    )


# ---------------------------------------------------------------------------
# beacon.validate — pure, no request/DB
# ---------------------------------------------------------------------------


class TestValidate:
    def test_accepts_each_closed_name_with_its_required_props(self):
        assert beacon.validate("pwa_installed", {"via": "appinstalled"}) == (
            "pwa_installed",
            {"via": "appinstalled"},
        )
        assert beacon.validate("push_permission", {"result": "granted"}) == (
            "push_permission",
            {"result": "granted"},
        )
        assert beacon.validate("push_clicked", {}) == ("push_clicked", {})

    def test_a_missing_props_is_treated_as_empty(self):
        assert beacon.validate("push_clicked", None) == ("push_clicked", {})

    def test_unknown_name_is_rejected_before_props_are_examined(self):
        # Garbage props alongside the unknown name still reports the name
        # as the problem — the name check runs first.
        name, error = beacon.validate("not_a_real_event", {"anything": "goes"})
        assert name is None
        assert error == "Unknown event name."

    def test_a_non_string_name_is_rejected(self):
        name, error = beacon.validate(None, {})
        assert name is None
        assert error == "Unknown event name."

    def test_props_must_be_an_object(self):
        name, error = beacon.validate("push_clicked", ["not", "a", "dict"])
        assert name is None
        assert error == "props must be an object."

    def test_unknown_prop_key_is_rejected(self):
        name, error = beacon.validate("push_clicked", {"extra": "value"})
        assert name is None
        assert error == "Unknown prop extra."

    def test_bad_prop_value_is_rejected(self):
        name, error = beacon.validate("pwa_installed", {"via": "typed-it-in"})
        assert name is None
        assert error == "Invalid value for via."

    def test_a_non_string_prop_value_is_rejected(self):
        name, error = beacon.validate("pwa_installed", {"via": 1})
        assert name is None
        assert error == "Invalid value for via."

    def test_missing_required_prop_is_rejected(self):
        name, error = beacon.validate("push_permission", {})
        assert name is None
        assert error == "Missing prop result."


# ---------------------------------------------------------------------------
# track_beacon — the HTTP surface
# ---------------------------------------------------------------------------


class TestTrackBeaconAcceptedEvents:
    @pytest.mark.parametrize(
        "name,props",
        [
            ("pwa_installed", {"via": "appinstalled"}),
            ("pwa_installed", {"via": "standalone"}),
            ("push_permission", {"result": "granted"}),
            ("push_permission", {"result": "denied"}),
            ("push_permission", {"result": "default"}),
            ("push_clicked", {}),
        ],
    )
    def test_each_accepted_name_is_recorded_as_a_client_event(
        self, client, name, props
    ):
        user = UserFactory()
        client.force_login(user)

        resp = _post(client, name, props)

        assert resp.status_code == 204
        assert resp.content == b""
        event = Event.objects.get()
        assert event.name == name
        assert event.actor == user
        assert event.props == props
        assert event.source == Event.Source.CLIENT

    def test_a_beacon_post_with_no_props_key_at_all_works_for_a_propless_name(
        self, client
    ):
        user = UserFactory()
        client.force_login(user)

        resp = client.post(
            _url(),
            data=json.dumps({"name": "push_clicked"}),
            content_type="application/json",
        )

        assert resp.status_code == 204
        assert Event.objects.get().props == {}


class TestTrackBeaconRejectsBadPayloads:
    def test_unknown_name_is_a_400_and_writes_nothing(self, client):
        client.force_login(UserFactory())

        resp = _post(client, "not_a_real_event", {})

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Unknown event name."}
        assert not Event.objects.exists()

    def test_unknown_prop_is_a_400_and_writes_nothing(self, client):
        client.force_login(UserFactory())

        resp = _post(client, "push_clicked", {"unexpected": "prop"})

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Unknown prop unexpected."}
        assert not Event.objects.exists()

    def test_bad_prop_value_is_a_400_and_writes_nothing(self, client):
        client.force_login(UserFactory())

        resp = _post(client, "pwa_installed", {"via": "not-a-real-source"})

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Invalid value for via."}
        assert not Event.objects.exists()

    def test_missing_required_prop_is_a_400_and_writes_nothing(self, client):
        client.force_login(UserFactory())

        resp = _post(client, "push_permission", {})

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Missing prop result."}
        assert not Event.objects.exists()


class TestTrackBeaconAnonymousAndExclusions:
    def test_anonymous_gets_204_and_writes_nothing(self, client):
        # No force_login: an anonymous visitor. There's nothing to redirect
        # to a login page for — see the view's module docstring — so this
        # is a silent no-op, not a 401/403/302.
        resp = _post(client, "push_clicked", {})

        assert resp.status_code == 204
        assert not Event.objects.exists()

    def test_a_staff_actor_is_dropped_silently(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = _post(client, "push_clicked", {})

        # The exclusion is `track()`'s contract (see test_track.py), not
        # this view's — asserted here as an outcome only.
        assert resp.status_code == 204
        assert not Event.objects.exists()

    def test_a_sandbox_coach_is_dropped_silently(self, client):
        client.force_login(sandbox.create_sandbox())

        resp = _post(client, "push_clicked", {})

        assert resp.status_code == 204
        assert not Event.objects.exists()


class TestTrackBeaconCsrf:
    def test_a_post_without_a_csrf_token_is_rejected(self):
        user = UserFactory()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(user)

        resp = _post(csrf_client, "push_clicked", {})

        assert resp.status_code == 403
        assert not Event.objects.exists()


class TestTrackBeaconRateLimit:
    def test_the_next_post_past_the_limit_is_429_and_writes_nothing(
        self, client, settings
    ):
        settings.ANALYTICS_BEACON_PER_USER_PER_HOUR = 3
        user = UserFactory()
        client.force_login(user)
        for _ in range(3):
            resp = _post(client, "push_clicked", {})
            assert resp.status_code == 204
        assert Event.objects.count() == 3

        resp = _post(client, "push_clicked", {})

        assert resp.status_code == 429
        assert resp.json() == {"ok": False, "error": "Too many events."}
        assert Event.objects.count() == 3  # nothing new

    def test_a_different_user_is_unaffected(self, client, settings):
        settings.ANALYTICS_BEACON_PER_USER_PER_HOUR = 1
        first = UserFactory()
        client.force_login(first)
        _post(client, "push_clicked", {})
        assert _post(client, "push_clicked", {}).status_code == 429

        other_client = Client()
        other_client.force_login(UserFactory())
        resp = _post(other_client, "push_clicked", {})

        assert resp.status_code == 204


class TestTrackBeaconMalformedRequests:
    def test_an_oversized_body_is_a_400_and_writes_nothing(self, client):
        client.force_login(UserFactory())
        body = json.dumps(
            {"name": "push_clicked", "props": {}, "pad": "x" * beacon.MAX_BODY_BYTES}
        )
        assert len(body.encode()) > beacon.MAX_BODY_BYTES

        resp = client.post(_url(), data=body, content_type="application/json")

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Body too large."}
        assert not Event.objects.exists()

    def test_malformed_json_is_a_400(self, client):
        client.force_login(UserFactory())

        resp = client.post(
            _url(), data="{not valid json", content_type="application/json"
        )

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Malformed JSON."}
        assert not Event.objects.exists()

    def test_a_non_object_body_is_a_400(self, client):
        client.force_login(UserFactory())

        resp = client.post(
            _url(),
            data=json.dumps(["not", "an", "object"]),
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Expected a JSON object."}
        assert not Event.objects.exists()

    def test_get_is_not_allowed(self, client):
        client.force_login(UserFactory())

        resp = client.get(_url())

        assert resp.status_code == 405
