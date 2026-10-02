"""Write-time unit snapshots for ``LoggedSet`` (#600)."""

import importlib
import json
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import AthleteProfileFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def logged_session(*, link=None, unit=Unit.POUNDS, load="235"):
    link = link or CoachAthleteFactory()
    plan = PlanFactory(
        relationship=link,
        status=Plan.Status.ACTIVE,
        unit=unit,
    )
    mesocycle = MesocycleFactory(plan=plan, order=0)
    week = WeekFactory(mesocycle=mesocycle, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name="Back Squat", text=f"3 x 5, {load}")
    return SimpleNamespace(
        link=link,
        plan=plan,
        session=session,
        cell=cell,
        load=load,
    )


def test_logged_set_unit_is_stamped_and_survives_a_plan_unit_change(client):
    fixture = logged_session()
    client.force_login(fixture.link.athlete)
    response = client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": fixture.session.pk}),
        data=json.dumps(
            {
                "exercise_id": fixture.cell.pk,
                "line": 1,
                "text": f"{fixture.load} x 5, RPE 8",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    logged_set = LoggedSet.objects.get(session_log__session=fixture.session)
    assert logged_set.unit == Unit.POUNDS

    fixture.plan.unit = Unit.KILOGRAMS
    fixture.plan.save(update_fields=["unit"])
    logged_set.refresh_from_db()

    assert logged_set.unit == Unit.POUNDS
    assert logged_set.load == fixture.load


def test_unit_changes_do_not_convert_existing_prescriptions_or_logged_loads():
    coach_profile = CoachProfileFactory(default_unit=Unit.POUNDS)
    link = CoachAthleteFactory(coach=coach_profile.user)
    athlete_profile = AthleteProfileFactory(user=link.athlete, unit=Unit.POUNDS)
    fixture = logged_session(link=link, unit=Unit.POUNDS, load="235.0")
    logged_set = LoggedSetFactory(
        session_log=SessionLogFactory(
            session=fixture.session,
            athlete=fixture.link.athlete,
            status=SessionLog.Status.DONE,
        ),
        prescription=fixture.cell,
        reps="5",
        load=fixture.load,
        unit=Unit.POUNDS,
    )

    fixture.plan.unit = Unit.KILOGRAMS
    fixture.plan.save(update_fields=["unit"])
    coach_profile.default_unit = Unit.KILOGRAMS
    coach_profile.save(update_fields=["default_unit"])
    athlete_profile.unit = Unit.KILOGRAMS
    athlete_profile.save(update_fields=["unit"])

    fixture.cell.refresh_from_db()
    logged_set.refresh_from_db()
    assert fixture.cell.text == "3 x 5, 235.0"
    assert logged_set.load == "235.0"
    assert logged_set.unit == Unit.POUNDS


def test_logged_set_unit_backfill_uses_each_rows_plan_unit():
    kg = logged_session(unit=Unit.KILOGRAMS, load="100")
    lb = logged_session(unit=Unit.POUNDS, load="225")
    kg_row = LoggedSetFactory(
        session_log=SessionLogFactory(
            session=kg.session,
            athlete=kg.link.athlete,
            status=SessionLog.Status.DONE,
        ),
        prescription=kg.cell,
        unit=Unit.POUNDS,
    )
    lb_row = LoggedSetFactory(
        session_log=SessionLogFactory(
            session=lb.session,
            athlete=lb.link.athlete,
            status=SessionLog.Status.DONE,
        ),
        prescription=lb.cell,
        unit=Unit.KILOGRAMS,
    )

    migration = importlib.import_module(
        "store_project.meso.migrations.0055_loggedset_unit"
    )
    migration.backfill_logged_set_units(
        django_apps, SimpleNamespace(connection=connection)
    )

    kg_row.refresh_from_db()
    lb_row.refresh_from_db()
    assert kg_row.unit == Unit.KILOGRAMS
    assert lb_row.unit == Unit.POUNDS
