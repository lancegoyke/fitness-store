"""``track()``: the one write path for first-party usage events (#509).

The per-event tests (one per call site) live beside the views they drive, in
``meso/tests/test_events.py``. This file pins the helper's own contract: one
row per call, sandbox and staff dropped, unknown names rejected, and never
an exception for the caller to handle.
"""

import logging
import uuid

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import DatabaseError

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.analytics.track import track
from store_project.meso import sandbox
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_writes_one_server_row_with_actor_subject_and_props():
    actor = UserFactory()
    subject = UserFactory()  # any saved model instance can be the subject

    result = track(EventName.PLAN_CREATED, actor=actor, subject=subject, draft=True)

    assert result is None  # nothing for a caller to depend on
    event = Event.objects.get()
    assert event.name == "plan_created"
    assert event.actor == actor
    assert event.subject_type == "users.user"
    assert event.subject_id == str(subject.pk)
    assert event.props == {"draft": True}
    assert event.source == Event.Source.SERVER
    assert event.created is not None


def test_actor_and_subject_are_optional():
    track(EventName.SUBSCRIPTION_CANCELLED)

    event = Event.objects.get()
    assert event.actor is None
    assert event.subject_type == ""
    assert event.subject_id == ""
    assert event.props == {}


def test_an_anonymous_actor_is_recorded_without_one():
    track(EventName.PLAN_CREATED, actor=AnonymousUser())

    assert Event.objects.get().actor is None


def test_uuid_props_are_stored_as_strings():
    athlete_id = uuid.uuid4()

    track(EventName.BLOCK_DELIVERED, actor=UserFactory(), athlete=athlete_id)

    assert Event.objects.get().props == {"athlete": str(athlete_id)}


def test_a_staff_actor_is_dropped_silently():
    track(EventName.PLAN_CREATED, actor=UserFactory(is_staff=True))

    assert not Event.objects.exists()


def test_a_sandbox_actor_is_dropped_silently():
    track(EventName.PLAN_CREATED, actor=sandbox.create_sandbox())

    assert not Event.objects.exists()


def test_the_test_settings_reject_unknown_names():
    """Pins the switch the next test relies on: strict everywhere under test."""
    from django.conf import settings

    assert settings.ANALYTICS_STRICT_EVENT_NAMES is True


def test_an_unknown_name_raises_when_strict(settings):
    settings.ANALYTICS_STRICT_EVENT_NAMES = True

    with pytest.raises(ValueError, match="plan_craeted"):
        track("plan_craeted", actor=UserFactory())

    assert not Event.objects.exists()


def test_an_unknown_name_is_checked_before_the_actor_exclusions(settings):
    """A typo on a path only staff exercise in tests must still fail loudly."""
    settings.ANALYTICS_STRICT_EVENT_NAMES = True

    with pytest.raises(ValueError):
        track("plan_craeted", actor=UserFactory(is_staff=True))


def test_an_unknown_name_is_logged_and_dropped_when_not_strict(settings, caplog):
    settings.ANALYTICS_STRICT_EVENT_NAMES = False

    with caplog.at_level(logging.ERROR, logger="store_project.analytics.track"):
        track("plan_craeted", actor=UserFactory())

    assert not Event.objects.exists()
    assert "plan_craeted" in caplog.text


def test_a_database_error_is_logged_never_raised(monkeypatch, caplog):
    def boom(**_fields):
        raise DatabaseError("insert failed")

    monkeypatch.setattr(Event.objects, "create", boom)

    with caplog.at_level(logging.ERROR, logger="store_project.analytics.track"):
        track(EventName.PLAN_CREATED, actor=UserFactory())

    assert "plan_created" in caplog.text


def test_any_other_error_is_logged_never_raised(monkeypatch, caplog):
    def boom(_user):
        raise RuntimeError("sandbox lookup broke")

    monkeypatch.setattr("store_project.analytics.track.is_sandbox", boom)

    with caplog.at_level(logging.ERROR, logger="store_project.analytics.track"):
        track(EventName.PLAN_CREATED, actor=UserFactory())

    assert not Event.objects.exists()
    assert "plan_created" in caplog.text


def test_every_event_name_is_snake_case_and_fits_the_column():
    max_length = Event._meta.get_field("name").max_length
    for value in EventName.values:
        assert value == value.lower()
        assert " " not in value
        assert len(value) <= max_length


def test_the_actor_fk_has_no_db_constraint():
    """No RI constraint on ``actor`` (#509 review).

    See ``test_track_postgres.py`` for why — a deferred FK would take a
    commit-time lock on the actor's row.
    """
    assert Event._meta.get_field("actor").db_constraint is False


def test_deleting_the_actor_nulls_it_and_keeps_the_row():
    actor = UserFactory()
    track(EventName.PLAN_CREATED, actor=actor)
    event = Event.objects.get()

    actor.delete()

    event.refresh_from_db()
    assert event.actor is None


def test_source_defaults_to_server():
    track(EventName.PLAN_CREATED, actor=UserFactory())

    assert Event.objects.get().source == Event.Source.SERVER


def test_source_client_is_honoured():
    track(EventName.PWA_INSTALLED, actor=UserFactory(), source=Event.Source.CLIENT)

    assert Event.objects.get().source == Event.Source.CLIENT


def test_a_prop_named_source_cannot_reach_the_props_column():
    """A dict-spread ``**props`` entry named ``source`` binds to the parameter.

    Python routes a keyword argument to a matching *named* parameter before
    it ever reaches ``**kwargs`` — true whether that parameter is keyword-only
    or not, and true whether the caller writes ``source=...`` directly or
    spreads a dict that happens to contain a ``"source"`` key
    (``track(name, actor=a, **{"source": "x"})`` behaves identically to
    ``track(name, actor=a, source="x")``). So a would-be prop literally named
    ``source`` can never land in the JSON ``props`` column as data — it is
    always consumed as provenance instead. This pins that as the actual,
    observable behaviour (not just the intent in the docstring), and it's
    exactly why ``track_beacon`` never spreads a raw request dict at
    ``track()``: it goes through ``beacon.validate`` first, whose closed prop
    set has no name that collides with ``track()``'s own keyword arguments.
    """
    actor = UserFactory()
    incoming = {"source": "google"}  # e.g. forwarded from somewhere else

    track(EventName.PLAN_CREATED, actor=actor, **incoming)

    event = Event.objects.get()
    assert event.source == "google"  # captured as provenance, not data
    assert event.props == {}  # never reached the props column
