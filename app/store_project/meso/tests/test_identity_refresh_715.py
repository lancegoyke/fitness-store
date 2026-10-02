"""#715 — an identity change refreshes the row's stored 1RM right away.

The athlete's %1RM suggestion reads ``AthleteOneRm`` by the row's CURRENT key.
Linking, renaming or swapping a row (or undoing one) gives it a key no stored
row exists for, so the suggestion was blank until the athlete's next finished
session even though #708's ``same_lift`` rule folds their history in.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.urls import reverse

from store_project.exercises.factories import ExerciseFactory
from store_project.meso import one_rm as meso_one_rm
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import Unit
from store_project.meso.tests.test_lift_identity_708 import BACK
from store_project.meso.tests.test_lift_identity_708 import FRONT
from store_project.meso.tests.test_lift_identity_708 import coach_patch
from store_project.meso.tests.test_lift_identity_708 import finish
from store_project.meso.tests.test_lift_identity_708 import fresh
from store_project.meso.tests.test_lift_identity_708 import logged_week1
from store_project.meso.tests.test_lift_identity_708 import rename_slot
from store_project.meso.tests.test_lift_identity_708 import seed_708
from store_project.meso.tests.test_lift_identity_708 import swap
from store_project.meso.tests.test_lift_identity_708 import type_line

pytestmark = pytest.mark.django_db


def shown(s, cell):
    """What the athlete's logger would read for ``cell`` right now."""
    row = meso_one_rm.one_rm_values(s.athlete, [fresh(cell)], Unit.KILOGRAMS).get(
        cell.pk
    )
    return None if row is None else (float(row.value), row.source)


def log_front_on_row2(client, s):
    """Front Squat 80 x 5 on day 2, so "Front Squat" has history elsewhere."""
    rename_slot(s.row2_w1, "Front Squat")
    client.force_login(s.athlete)
    assert type_line(client, s.d2w1, s.row2_w1, "80 x 5").status_code == 200
    finish(s.d2w1, s.athlete)


def test_linking_a_free_text_row_shows_its_history_at_once(
    client, django_capture_on_commit_callbacks
):
    s = seed_708()
    logged_week1(client, s)
    ex = ExerciseFactory(name="Back Squat", slug="back-squat")
    assert shown(s, s.row1_w1) is None
    with django_capture_on_commit_callbacks(execute=True):
        coach_patch(
            client, s, s.row1_w1, {"name": "Back Squat", "exercise_id": str(ex.pk)}
        )
    got = shown(s, s.row1_w1)
    assert got is not None, "linked row's 1RM is blank until the next finished session"
    assert got[0] == pytest.approx(BACK, abs=0.05)


def test_renaming_to_a_lift_with_history_elsewhere_fills_at_once(
    client, django_capture_on_commit_callbacks
):
    s = seed_708()
    log_front_on_row2(client, s)
    assert shown(s, s.row1_w1) is None
    with django_capture_on_commit_callbacks(execute=True):
        coach_patch(client, s, s.row1_w1, {"name": "Front Squat"})
    got = shown(s, s.row1_w1)
    assert got is not None, "renamed row's 1RM is blank"
    assert got[0] == pytest.approx(FRONT, abs=0.05)


def test_agent_swap_takes_the_new_lifts_own_history_only(
    client, django_capture_on_commit_callbacks
):
    s = seed_708()
    logged_week1(client, s)  # back squat 140 x 5 on the row about to be swapped
    log_front_on_row2(client, s)
    with django_capture_on_commit_callbacks(execute=True):
        swap(s)
    got = shown(s, s.row1_w1)
    assert got is not None, "swapped row's 1RM is blank"
    assert got[0] == pytest.approx(FRONT, abs=0.05), (
        f"the old lift's sets leaked into the swapped lift: {got}"
    )


def test_a_manual_1rm_survives_an_identity_change(
    client, django_capture_on_commit_callbacks
):
    s = seed_708()
    log_front_on_row2(client, s)
    AthleteOneRm.objects.create(
        athlete=s.athlete,
        name="Front Squat",
        value=Decimal("250"),
        unit=Unit.KILOGRAMS,
        source=AthleteOneRm.Source.MANUAL,
    )
    with django_capture_on_commit_callbacks(execute=True):
        coach_patch(client, s, s.row1_w1, {"name": "Front Squat"})
    assert shown(s, s.row1_w1) == (250.0, "manual")


def test_undo_of_an_identity_change_refreshes_the_restored_identity(
    client, django_capture_on_commit_callbacks
):
    s = seed_708()
    logged_week1(client, s)
    ex = ExerciseFactory(name="Back Squat", slug="back-squat")
    with django_capture_on_commit_callbacks(execute=True):
        coach_patch(
            client, s, s.row1_w1, {"name": "Back Squat", "exercise_id": str(ex.pk)}
        )
    AthleteOneRm.objects.all().delete()  # nothing stored under the restored key
    client.force_login(s.coach)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk}),
            content_type="application/json",
        )
    assert resp.status_code == 200, resp.content
    got = shown(s, s.row1_w1)
    assert got is not None, "undo left the restored identity without a 1RM"
    assert got[0] == pytest.approx(BACK, abs=0.05)


def test_a_template_plan_has_no_athlete_and_queues_nothing(
    django_capture_on_commit_callbacks,
):
    template = SimpleNamespace(athlete=None, unit=Unit.KILOGRAMS)
    slot = SimpleNamespace(exercise_id=None, name="Back Squat")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        meso_one_rm.refresh_after_identity_change(template, [slot])
    assert callbacks == []
