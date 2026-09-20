"""PostgreSQL regression for admin hard-delete parent locking (#587)."""

import threading

import pytest
from django.contrib import admin
from django.db import connection
from django.db import transaction
from django.db.models.signals import post_delete
from django.db.utils import OperationalError

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.users.admin import UserAdmin
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row locks are not observable on SQLite.",
    ),
]


def test_user_admin_delete_model_prelocks_plan_before_the_cascade():
    coach = UserFactory()
    athlete = UserFactory()
    link = CoachAthleteFactory(coach=coach, athlete=athlete)
    plan = PlanFactory(relationship=link)
    batch = AgentProposalBatchFactory(plan=plan, coach=coach)
    change = ProposedChangeFactory(batch=batch)
    reached_child_delete = threading.Event()
    release_delete = threading.Event()
    delete_errors = []

    def pause_after_child_delete(sender, instance, **kwargs):
        if instance.pk != change.pk:
            return
        reached_child_delete.set()
        assert release_delete.wait(timeout=8)

    uid = f"test-587-admin-delete-{change.pk}"
    post_delete.connect(
        pause_after_child_delete,
        sender=ProposedChange,
        dispatch_uid=uid,
        weak=False,
    )

    def run_delete():
        try:
            UserAdmin(User, admin.site).delete_model(None, coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            delete_errors.append(exc)
        finally:
            connection.close()

    deleter = threading.Thread(target=run_delete)
    try:
        deleter.start()
        assert reached_child_delete.wait(timeout=5), (
            "the admin cascade never reached ProposedChange.post_delete"
        )
        plan_was_prelocked = False
        try:
            with transaction.atomic():
                Plan.objects.select_for_update(nowait=True).get(pk=plan.pk)
        except OperationalError:
            plan_was_prelocked = True
        release_delete.set()
        deleter.join(timeout=10)
    finally:
        release_delete.set()
        post_delete.disconnect(
            pause_after_child_delete,
            sender=ProposedChange,
            dispatch_uid=uid,
        )

    assert plan_was_prelocked, (
        "UserAdmin began deleting children before reserving the parent Plan row"
    )
    assert not deleter.is_alive()
    assert delete_errors == []
    assert not User.objects.filter(pk=coach.pk).exists()
