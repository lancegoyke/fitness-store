"""SQLite-visible lock-order wiring regressions for #587, #588, and #596."""

from unittest import mock

import pytest
from django.contrib import admin
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.urls import reverse

from store_project.meso import demo
from store_project.meso import views
from store_project.meso.admin import AgentProposalBatchAdmin
from store_project.meso.admin import CoachAthleteAdmin
from store_project.meso.admin import PlanAdmin
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import Plan
from store_project.users.admin import UserAdmin
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


ADMIN_CASES = [
    (UserAdmin, User, UserFactory, "lock_cascade_parents"),
    (
        CoachAthleteAdmin,
        CoachAthlete,
        CoachAthleteFactory,
        "lock_cascade_from_links",
    ),
    (PlanAdmin, Plan, PlanFactory, "lock_cascade_from_plans"),
    (
        AgentProposalBatchAdmin,
        AgentProposalBatch,
        AgentProposalBatchFactory,
        "lock_cascade_from_batches",
    ),
]


@pytest.mark.parametrize(
    ("admin_class", "model", "factory", "helper_name"), ADMIN_CASES
)
def test_admin_delete_model_locks_the_matching_cascade_root(
    admin_class, model, factory, helper_name
):
    obj = factory()
    pk = obj.pk
    with mock.patch.object(demo, helper_name, create=True) as lock_helper:
        admin_class(model, admin.site).delete_model(None, obj)

    lock_helper.assert_called_once_with([pk])
    assert not model.objects.filter(pk=pk).exists()


@pytest.mark.parametrize(
    ("admin_class", "model", "factory", "helper_name"), ADMIN_CASES
)
def test_admin_delete_queryset_locks_all_matching_cascade_roots(
    admin_class, model, factory, helper_name
):
    objects = [factory(), factory()]
    pks = sorted(obj.pk for obj in objects)
    queryset = model.objects.filter(pk__in=pks)
    with mock.patch.object(demo, helper_name, create=True) as lock_helper:
        admin_class(model, admin.site).delete_queryset(None, queryset)

    lock_helper.assert_called_once_with(pks)
    assert not model.objects.filter(pk__in=pks).exists()


def test_merge_users_locks_the_source_cascade_before_deleting(monkeypatch):
    source = UserFactory(email="source-merge@example.com")
    target = UserFactory(email="target-merge@example.com")
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")

    with mock.patch.object(demo, "lock_cascade_parents") as lock_helper:
        call_command("merge_users", source.email, target.email, verbosity=0)

    lock_helper.assert_called_once_with([source.pk])
    assert not User.objects.filter(pk=source.pk).exists()
    assert User.objects.filter(pk=target.pk).exists()


def test_invite_accept_404s_if_the_invite_coach_changes_before_lock(
    client, monkeypatch
):
    original = CoachInviteFactory()
    changed = CoachInviteFactory()
    claimant = UserFactory()
    client.force_login(claimant)
    returned = iter([original, changed])
    monkeypatch.setattr(
        views, "get_object_or_404", lambda *args, **kwargs: next(returned)
    )
    monkeypatch.setattr(views.billing_access, "can_add_athlete", lambda coach: True)

    response = client.post(
        reverse("meso:invite_claim", kwargs={"token": original.token}),
        {"action": "accept"},
    )

    assert response.status_code == 404
    assert not CoachAthlete.objects.filter(
        coach=changed.coach, athlete=claimant
    ).exists()


def test_athlete_request_coach_handles_coach_gone_before_user_locks(
    client, monkeypatch
):
    coach = CoachProfileFactory().user
    athlete = UserFactory()
    client.force_login(athlete)
    monkeypatch.setattr(
        views.User.objects,
        "select_for_update",
        lambda **kwargs: User.objects.filter(pk=athlete.pk),
    )

    response = client.post(
        reverse("meso:athlete_request_coach"), {"email": coach.email}
    )

    assert response.status_code == 302
    assert response.url == reverse("meso:athlete_home")
    assert not CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()
    messages = [message.message for message in get_messages(response.wsgi_request)]
    assert "We couldn't find a coach with that email." in messages


def test_coach_invite_404s_if_the_coach_is_gone_before_lock(client, monkeypatch):
    coach = UserFactory()
    client.force_login(coach)
    monkeypatch.setattr(
        views.User.objects,
        "select_for_update",
        lambda **kwargs: User.objects.none(),
    )

    response = client.post(
        reverse("meso:coach_invite"), {"email": "athlete@example.com"}
    )

    assert response.status_code == 404
    assert not CoachInvite.objects.filter(coach=coach).exists()
