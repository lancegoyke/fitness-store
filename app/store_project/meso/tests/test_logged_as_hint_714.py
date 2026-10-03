"""#714 -- a swapped/renamed row says what the athlete's sets were logged as.

After an agent swap (Back Squat -> Front Squat) the week-1 sets keep their #708
stamp ("Back Squat") while every plan-shaped surface labels the row by the slot's
current name. Three surfaces carry an additive ``logged_as`` (names) /
``logged_as_mixed`` pair so the UI can show a quiet "logged as Back Squat" hint:
the coach's session results rows, the designer grid cell's ``athlete_summary``
and the athlete page's ``exercises[]`` (first paint and live sync). Display only.
"""

from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from store_project.exercises.factories import ExerciseFactory
from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso.lift_identity import Lift
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.tests.test_lift_identity_708 import finish
from store_project.meso.tests.test_lift_identity_708 import link_slot
from store_project.meso.tests.test_lift_identity_708 import seed_708
from store_project.meso.tests.test_lift_identity_708 import swap
from store_project.meso.tests.test_lift_identity_708 import type_line

pytestmark = pytest.mark.django_db


def log_back_squat(client, s, text="140 x 5 @9"):
    client.force_login(s.athlete)
    assert type_line(client, s.d1w1, s.row1_w1, text).status_code == 200
    finish(s.d1w1, s.athlete)


def rename_slot(s, name):
    slot = s.slot1
    slot.name = name
    slot.save(update_fields=["name"])


def result_row(s):
    return presenters.session_results(s.d1w1)["rows"][0]


def grid_summary(s):
    meso = Plan.objects.get(pk=s.plan.pk).mesocycles.get()
    grid = serializers.serialize_mesocycle_grid(meso)
    cells = grid["days"][0]["rows"][0]["cells"]
    return cells[str(s.row1_w1.week_id)]["athlete_summary"]


def athlete_ex(s):
    return presenters.athlete_session(s.d1w1, s.athlete)["exercises"][0]


def surfaces(s):
    """(logged_as, mixed) of the three surfaces, in a fixed order."""
    row, summ, ex = result_row(s), grid_summary(s), athlete_ex(s)
    return [
        (row["logged_as"], row["logged_as_mixed"]),
        (summ["logged_as"], summ["logged_as_mixed"]),
        (ex["logged_as"], ex["logged_as_mixed"]),
    ]


class TestSwapHint:
    def test_every_surface_names_the_lift_actually_logged(self, client):
        s = seed_708()
        log_back_squat(client, s)
        swap(s)
        assert result_row(s)["name"] == "Front Squat"
        assert surfaces(s) == [(["Back Squat"], False)] * 3

    def test_summary_flag_names_the_lift_performed(self, client):
        s = seed_708()
        s.row1_w1.text = "3 x 5, RPE 7"
        s.row1_w1.save()
        log_back_squat(client, s, "140 x 5 @9")
        swap(s)
        flag = presenters.session_results(s.d1w1)["summary"]["flag"]
        assert flag.startswith("Back Squat ran 2 RPE over target"), flag

    def test_flag_keeps_current_name_without_a_hint(self, client):
        s = seed_708()
        s.row1_w1.text = "3 x 5, RPE 7"
        s.row1_w1.save()
        log_back_squat(client, s, "140 x 5 @9")
        flag = presenters.session_results(s.d1w1)["summary"]["flag"]
        assert flag.startswith("Back Squat ran 2 RPE over target"), flag

    def test_athlete_payload_and_sync_carry_it(self, client):
        s = seed_708()
        log_back_squat(client, s)
        swap(s)
        payload = presenters.athlete_log_payload(
            presenters.athlete_session(s.d1w1, s.athlete)
        )
        assert payload["exercises"][0]["logged_as"] == ["Back Squat"]
        assert payload["exercises"][0]["logged_as_mixed"] is False
        resp = client.get(
            reverse("meso:athlete_session_sync", kwargs={"pk": s.d1w1.pk}),
            {"v": 0},
        )
        assert resp.status_code == 200
        ex = resp.json()["exercises"][0]
        assert ex["logged_as"] == ["Back Squat"]
        assert ex["logged_as_mixed"] is False

    def test_grid_cell_response_for_a_single_cell_carries_it(self, client):
        s = seed_708()
        log_back_squat(client, s)
        swap(s)
        cell = serializers.grid_cell_for(s.slot1, s.row1_w1.week)
        assert cell["athlete_summary"]["logged_as"] == ["Back Squat"]


class TestMixed:
    def test_sets_under_both_lifts_are_mixed(self, client):
        s = seed_708()
        log_back_squat(client, s)
        swap(s)
        assert type_line(client, s.d1w1, s.row1_w1, "80 x 5", line=2).status_code == 200
        assert LoggedSet.objects.filter(exercise_name="Front Squat").count() == 1
        assert surfaces(s) == [(["Back Squat"], True)] * 3


class TestNoHint:
    def test_linking_the_same_name_to_the_catalog_is_not_a_swap(self, client):
        s = seed_708()
        log_back_squat(client, s)
        link_slot(s.row1_w1, ExerciseFactory(name="Back Squat", slug="back-squat"))
        assert surfaces(s) == [([], False)] * 3

    def test_case_and_whitespace_only_rename_is_not_a_swap(self, client):
        s = seed_708()
        log_back_squat(client, s)
        rename_slot(s, "back squat ")
        assert surfaces(s) == [([], False)] * 3

    def test_an_unstamped_set_reads_as_the_rows_identity(self, client):
        s = seed_708()
        log_back_squat(client, s)
        LoggedSet.objects.update(exercise=None, exercise_name=None)
        swap(s)
        assert surfaces(s) == [([], False)] * 3

    def test_never_swapped_row_has_no_hint(self, client):
        s = seed_708()
        log_back_squat(client, s)
        assert surfaces(s) == [([], False)] * 3

    def test_unlogged_row_has_no_hint(self):
        s = seed_708()
        assert result_row(s)["logged_as"] == []
        assert athlete_ex(s)["logged_as"] == []
        assert grid_summary(s) is None


class TestGridQueryCount:
    def test_the_hint_adds_no_queries_to_the_grid_payload(self, client):
        s = seed_708()
        log_back_squat(client, s)
        swap(s)
        meso = Plan.objects.get(pk=s.plan.pk).mesocycles.get()
        with CaptureQueriesContext(connection) as ctx:
            grid = serializers.serialize_mesocycle_grid(meso)
        # One more exercise row of logged cells must not add a query per cell.
        summ = grid["days"][0]["rows"][0]["cells"][str(s.row1_w1.week_id)][
            "athlete_summary"
        ]
        assert summ["logged_as"] == ["Back Squat"]
        base = len(ctx)
        from store_project.meso.tests._helpers import presc

        for i in range(3):
            extra = presc(s.d1w1, name=f"Extra {i}", order=i + 1, text="3 x 5")
            assert type_line(client, s.d1w1, extra, "50 x 5").status_code == 200
        with CaptureQueriesContext(connection) as ctx2:
            serializers.serialize_mesocycle_grid(meso)
        assert len(ctx2) == base, (base, len(ctx2))


class TestForeignLiftNames:
    def sets(self, *pairs):
        return [SimpleNamespace(exercise_id=i, exercise_name=n) for i, n in pairs]

    def test_names_and_mixed(self):
        from store_project.meso.lift_identity import foreign_lift_names

        target = Lift(None, "Front Squat")
        assert foreign_lift_names(target, self.sets((None, "Back Squat"))) == (
            ["Back Squat"],
            False,
        )
        got = foreign_lift_names(
            target,
            self.sets(
                (None, "Back Squat"), (None, "front squat"), (None, " back SQUAT")
            ),
        )
        assert got == (["Back Squat"], True)

    def test_no_foreign_sets(self):
        from store_project.meso.lift_identity import foreign_lift_names

        target = Lift(None, "Back Squat")
        assert foreign_lift_names(target, []) == ([], False)
        assert foreign_lift_names(target, self.sets((None, "back squat "))) == (
            [],
            False,
        )
        assert foreign_lift_names(target, self.sets((7, "Back Squat"))) == ([], False)

    def test_unstamped_never_yields_and_counts_as_not_foreign(self):
        from store_project.meso.lift_identity import foreign_lift_names

        target = Lift(None, "Front Squat")
        # An unstamped set must not even have its .lift read.
        unstamped = SimpleNamespace(exercise_id=None, exercise_name=None)
        assert foreign_lift_names(target, [unstamped]) == ([], False)
        assert foreign_lift_names(
            target, [unstamped, *self.sets((None, "Back Squat"))]
        ) == (["Back Squat"], True)

    def test_different_catalog_ids_are_foreign_under_one_name(self):
        from store_project.meso.lift_identity import foreign_lift_names

        got = foreign_lift_names(Lift(1, "Squat"), self.sets((2, "Squat")))
        assert got == (["Squat"], False)

    def test_catalog_lift_known_under_another_name(self):
        from store_project.meso.lift_identity import foreign_lift_names

        # Free-text "Back Squat" later linked to catalog E, and E now shows as
        # "Squat": one lift under the module's alias rule, not a swap.
        target = Lift(7, "Squat")
        got = foreign_lift_names(
            target, self.sets((None, "Back Squat"), (7, "Back Squat"))
        )
        assert got == ([], False)
        # Without E ever stamped under that name it IS a different lift.
        assert foreign_lift_names(target, self.sets((None, "Back Squat"))) == (
            ["Back Squat"],
            False,
        )
