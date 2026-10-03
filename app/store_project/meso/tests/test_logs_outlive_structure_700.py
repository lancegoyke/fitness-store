"""#700 — logs outlive structure.

``SessionLog.session`` and ``LoggedSet.exercise_slot`` are ``RESTRICT``: a hard
delete of plan structure (Plan, Mesocycle, Week, Session, SessionSlot,
ExerciseSlot, CoachAthlete, or a coach User) refuses with ``RestrictedError``
while an athlete's log hangs below it. ``SessionLog.athlete`` and
``LoggedSet.session_log`` stay CASCADE, so a log the SAME delete also reaches
through its own athlete is still removed (RESTRICT, unlike PROTECT, allows that).

The reseed rebuild is covered in ``test_seed_demo.py::TestReseedReconciles``;
``merge_users`` in ``users/tests/test_merge_users.py``.
"""

import logging

import pytest
from django.db import transaction
from django.db.models import RestrictedError
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso.admin import MesocycleInline
from store_project.meso.admin import SessionInline
from store_project.meso.admin import WeekInline
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import SandboxSession
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db

PROTECTED = "would require deleting the following protected related objects"


def _logged(s):
    """A realistic athlete log on ``s``'s plan: the athlete's SessionLog + a set."""
    log = SessionLogFactory(
        session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
    )
    row = LoggedSetFactory(
        session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
    )
    assert row.exercise_slot_id == s.squat.exercise_slot_id
    return log, row


def _assert_logs_survive(log, row):
    assert SessionLog.objects.filter(pk=log.pk).exists()
    assert LoggedSet.objects.filter(pk=row.pk).exists()


ROOTS = {
    "plan": lambda s: s.plan,
    "mesocycle": lambda s: s.meso,
    "week": lambda s: s.week,
    "session": lambda s: s.session,
    "session_slot": lambda s: s.session.session_slot,
    "exercise_slot": lambda s: s.squat.exercise_slot,
    "coach_athlete": lambda s: s.rel,
    "coach_user": lambda s: s.coach,
}


class TestStructureHardDeleteRefuses:
    @pytest.mark.parametrize("root_name", list(ROOTS))
    def test_refuses_while_an_athletes_log_hangs_below(self, root_name):
        s = seed()
        log, row = _logged(s)
        root = ROOTS[root_name](s)

        with pytest.raises(RestrictedError):
            with transaction.atomic():
                root.delete()

        assert type(root).objects.filter(pk=root.pk).exists()
        _assert_logs_survive(log, row)


class TestOwnAccountDeleteTakesOwnLogs:
    def test_deleting_the_athlete_removes_their_logs_only(self):
        s = seed()
        log, row = _logged(s)

        s.athlete.delete()

        assert not SessionLog.objects.filter(pk=log.pk).exists()
        assert not LoggedSet.objects.filter(pk=row.pk).exists()
        assert User.objects.filter(pk=s.coach.pk).exists()
        assert not CoachAthlete.objects.filter(pk=s.rel.pk).exists()

    def test_deleting_a_self_coached_user_succeeds(self):
        user = UserFactory()
        link = CoachAthlete.add_self(user)
        plan = PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
        meso = MesocycleFactory(plan=plan)
        week = WeekFactory(mesocycle=meso, index=1)
        session = day(week, day_number=1, name="Day")
        cell = presc(session, name="Squat")
        log = SessionLogFactory(session=session, athlete=user)
        row = LoggedSetFactory(session_log=log, prescription=cell)
        assert row.exercise_slot_id == cell.exercise_slot_id

        user.delete()

        assert not User.objects.filter(pk=user.pk).exists()
        assert not SessionLog.objects.filter(pk=log.pk).exists()
        assert not LoggedSet.objects.filter(pk=row.pk).exists()
        assert not Plan.objects.filter(pk=plan.pk).exists()


def _demo_coach():
    coach = UserFactory()
    CoachProfile.objects.get_or_create(user=coach)
    return coach


class TestDemoAndSandboxPathsStillWork:
    def test_clear_demo_after_load_demo_removes_the_demo_logs(self):
        coach = _demo_coach()
        demo.load_demo(coach)
        athletes = list(demo._demo_athletes(coach))
        assert athletes
        logs = SessionLog.objects.filter(athlete__in=athletes)
        assert logs.exists(), "precondition: load_demo logs history"
        # A coach-entered set on a demo athlete's log rides the same cascade.
        ls = LoggedSet.objects.filter(session_log__in=logs).first()
        LoggedSet.objects.filter(pk=ls.pk).update(entered_by_coach=True)

        demo.clear_demo(coach)

        assert not SessionLog.objects.filter(athlete__in=athletes).exists()
        assert not LoggedSet.objects.filter(session_log__athlete__in=athletes).exists()
        assert User.objects.filter(pk=coach.pk).exists()

    def test_sandbox_reap_with_demo_and_self_coached_logs(self, caplog):
        user = sandbox.create_sandbox()
        athlete_ids = [u.pk for u in demo._demo_athletes(user)]
        assert SessionLog.objects.filter(athlete_id__in=athlete_ids).exists()

        link = CoachAthlete.add_self(user)
        plan = PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
        meso = MesocycleFactory(plan=plan)
        week = WeekFactory(mesocycle=meso, index=1)
        session = day(week, day_number=1, name="Day")
        cell = presc(session, name="Squat")
        log = SessionLogFactory(session=session, athlete=user)
        row = LoggedSetFactory(session_log=log, prescription=cell)
        assert row.exercise_slot_id == cell.exercise_slot_id

        SandboxSession.objects.filter(user=user).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1)
        )
        caplog.set_level(logging.ERROR, logger=sandbox.logger.name)

        assert sandbox.expire_sandboxes() == 1

        assert not User.objects.filter(pk=user.pk).exists()
        assert not User.objects.filter(pk__in=athlete_ids).exists()
        assert not SessionLog.objects.filter(
            athlete_id__in=[user.pk, *athlete_ids]
        ).exists()
        assert not LoggedSet.objects.filter(
            session_log__athlete_id__in=[user.pk, *athlete_ids]
        ).exists()
        assert not any(
            "Failed to reap sandbox" in r.getMessage() for r in caplog.records
        )


def _admin_client(client):
    client.force_login(SuperAdminFactory())
    return client


class TestAdminDeleteShowsRestrictedPage:
    def test_plan_delete_page_lists_the_protected_logs(self, client):
        s = seed()
        log, row = _logged(s)
        _admin_client(client)
        url = reverse("admin:meso_plan_delete", args=[s.plan.pk])

        assert PROTECTED in client.get(url).content.decode()
        resp = client.post(url, {"post": "yes"})

        assert resp.status_code == 200
        assert PROTECTED in resp.content.decode()
        assert Plan.objects.filter(pk=s.plan.pk).exists()
        _assert_logs_survive(log, row)

    def test_mesocycle_delete_page_lists_the_protected_logs(self, client):
        s = seed()
        log, row = _logged(s)
        _admin_client(client)
        url = reverse("admin:meso_mesocycle_delete", args=[s.meso.pk])

        assert PROTECTED in client.get(url).content.decode()
        resp = client.post(url, {"post": "yes"})

        assert resp.status_code == 200
        assert Mesocycle.objects.filter(pk=s.meso.pk).exists()
        _assert_logs_survive(log, row)

    def test_coach_user_delete_page_lists_the_protected_logs(self, client):
        s = seed()
        log, row = _logged(s)
        _admin_client(client)
        meta = User._meta
        url = reverse(
            f"admin:{meta.app_label}_{meta.model_name}_delete", args=[s.coach.pk]
        )

        assert PROTECTED in client.get(url).content.decode()
        resp = client.post(url, {"post": "yes"})

        assert resp.status_code == 200
        assert User.objects.filter(pk=s.coach.pk).exists()
        _assert_logs_survive(log, row)

    def test_delete_selected_action_refuses_on_plans(self, client):
        s = seed()
        log, row = _logged(s)
        _admin_client(client)
        url = reverse("admin:meso_plan_changelist")
        data = {"action": "delete_selected", "_selected_action": [s.plan.pk]}

        resp = client.post(url, data)
        assert resp.status_code == 200
        assert "Cannot delete Plan" in resp.content.decode()
        assert PROTECTED in resp.content.decode()

        resp = client.post(url, {**data, "post": "yes"})
        assert resp.status_code == 200
        assert Plan.objects.filter(pk=s.plan.pk).exists()
        _assert_logs_survive(log, row)


def _post_data_from(response):
    """Rebuild a change form's POST payload from the rendered admin context."""
    from django.forms.widgets import MultiWidget

    data = {}

    def add(form):
        for bf in form:
            value = bf.value()
            widget = bf.field.widget
            name = bf.html_name
            if isinstance(widget, MultiWidget):
                for i, part in enumerate(widget.decompress(value)):
                    if part not in (None, ""):
                        data[f"{name}_{i}"] = part
                    else:
                        data[f"{name}_{i}"] = ""
            elif value is None or value is False:
                continue
            elif value is True:
                data[name] = "on"
            elif isinstance(value, (list, tuple)):
                data[name] = [str(v) for v in value]
            else:
                data[name] = str(value)

    add(response.context["adminform"].form)
    for iaf in response.context["inline_admin_formsets"]:
        fs = iaf.formset
        for key, value in fs.management_form.initial.items():
            data[f"{fs.prefix}-{key}"] = str(value)
        for form in fs.forms:
            add(form)
    data["_save"] = "Save"
    return data


class TestAdminInlineCannotDeleteStructure:
    def test_inlines_are_not_deletable(self):
        assert MesocycleInline.can_delete is False
        assert WeekInline.can_delete is False
        assert SessionInline.can_delete is False

    def test_a_crafted_delete_on_the_mesocycle_inline_leaves_everything(self, client):
        s = seed()
        log, row = _logged(s)
        _admin_client(client)
        url = reverse("admin:meso_plan_change", args=[s.plan.pk])
        page = client.get(url)
        assert page.status_code == 200
        data = _post_data_from(page)
        # The checkbox is never rendered (can_delete = False); a crafted POST
        # adds it by hand to prove nothing acts on it.
        data["mesocycles-0-DELETE"] = "on"

        resp = client.post(url, data)

        if resp.status_code != 302:
            errors = [i.formset.errors for i in resp.context["inline_admin_formsets"]]
            raise AssertionError(
                f"expected a clean save (302), got {resp.status_code}: "
                f"{resp.context['adminform'].form.errors!r} {errors!r}"
            )
        assert Mesocycle.objects.filter(pk=s.meso.pk).exists()
        _assert_logs_survive(log, row)
