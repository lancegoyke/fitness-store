"""#578 stage 4 — the demo and seed log sets the way an athlete's TYPED lines do.

The structured Set-row logger is retired: an athlete logs by typing a line
(``views.athlete_cell_write`` -> ``_upsert_parsed_set``), which writes an
``athlete_authored`` sub-line cell plus a ``LoggedSet`` whose ``source_line``
points at it. The demo's sandbox log and the seed command's logs (the sample
session and every history week) must have exactly that shape, or the designer's
athlete-line marker, the coach's results and the athlete page show them as
strays.
"""

import pytest
from django.core.management import call_command

from store_project.meso import demo
from store_project.meso import views
from store_project.meso.factories import UserFactory
from store_project.meso.management.commands import seed_meso_demo
from store_project.meso.models import CoachProfile
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.parsing import performed_text_shows

pytestmark = pytest.mark.django_db

COACH_EMAIL = "coach@example.test"


def _assert_typed_shape(sets):
    assert sets, "expected some logged sets"
    for s in sets:
        cell = s.source_line
        assert cell is not None, f"{s} is structured-origin"
        assert cell.athlete_authored is True
        assert cell.line >= 1
        assert cell.exercise_slot_id == s.prescription.exercise_slot_id
        assert s.exercise_slot_id == s.prescription.exercise_slot_id
        assert cell.week_id == s.session_log.session.week_id
        assert s.set_number == cell.line
        assert performed_text_shows(cell.text, reps=s.reps, load=s.load, rpe=s.rpe)


def _demo_coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


def _fingerprint():
    return (
        Prescription.objects.count(),
        LoggedSet.objects.count(),
        SessionLog.objects.count(),
    )


def test_cell_line_cap_mirrors_views():
    assert seed_meso_demo.MAX_CELL_LINE == views.MAX_CELL_LINE


class TestSandboxDemoLog:
    def test_every_set_is_typed_origin(self):
        coach = _demo_coach()
        demo.load_log(coach)
        sets = list(
            LoggedSet.objects.filter(
                session_log__athlete__in=demo._demo_athletes(coach)
            ).select_related("source_line", "prescription", "session_log__session")
        )
        assert len(sets) == 17
        _assert_typed_shape(sets)
        assert not LoggedSet.objects.filter(source_line__isnull=True).exists()

    def test_sample_texts(self):
        coach = _demo_coach()
        demo.load_log(coach)
        texts = set(
            Prescription.objects.filter(athlete_authored=True).values_list(
                "text", flat=True
            )
        )
        assert {
            "70 x 6, RPE 7",
            "70 x 6, RPE 8.5",
            "18 x 10, RPE 7",
            "60 x 15",
            "41 x 9, RPE 8.5",
        } <= texts

    def test_rerun_adds_nothing(self):
        coach = _demo_coach()
        demo.load_log(coach)
        before = _fingerprint()
        demo.load_log(coach)
        assert _fingerprint() == before


class TestSeedCommand:
    def _seed(self):
        call_command("seed_meso_demo", coach_email=COACH_EMAIL)

    def test_every_set_in_every_log_is_typed_origin(self):
        self._seed()
        sets = list(
            LoggedSet.objects.select_related(
                "source_line", "prescription", "session_log__session"
            )
        )
        assert LoggedSet.objects.count() > 17  # history, not just the sample
        _assert_typed_shape(sets)
        assert not LoggedSet.objects.filter(source_line__isnull=True).exists()

    def test_coach_authored_sub_line_is_untouched(self):
        self._seed()
        # The generator's RPE cue (line 1) sits under logged cells; athlete
        # lines must route around it, never over it.
        cues = Prescription.objects.filter(line=1, athlete_authored=False).exclude(
            text=""
        )
        cue_ids = set(
            cues.filter(
                exercise_slot__in=LoggedSet.objects.values("exercise_slot")
            ).values_list("pk", flat=True)
        )
        assert cue_ids
        snapshot = dict(
            Prescription.objects.filter(pk__in=cue_ids).values_list("pk", "text")
        )
        call_command("seed_meso_demo", coach_email=COACH_EMAIL)
        for cue in Prescription.objects.filter(pk__in=cue_ids):
            assert cue.text == snapshot[cue.pk]
            assert cue.athlete_authored is False
        # ...and no logged set sits on a coach-authored cell.
        assert not LoggedSet.objects.filter(source_line__pk__in=cue_ids).exists()

    def test_rerun_adds_nothing(self):
        self._seed()
        before = _fingerprint()
        self._seed()
        assert _fingerprint() == before


class TestHelper:
    def test_never_overwrites_coach_line_and_skips_non_sets(self):
        from store_project.meso.factories import SessionLogFactory
        from store_project.meso.factories import WeekFactory

        from ._helpers import day
        from ._helpers import presc
        from ._helpers import sub_line

        session = day(WeekFactory(), day_number=1, name="Lower")
        cell = presc(session, text="3 x 8, 60")
        cue = sub_line(cell, "RPE 7")
        log = SessionLogFactory(session=session)

        rows = seed_meso_demo.log_typed_sets(
            log, [(cell, ["60 x 8", "felt heavy", "BW x 12, RPE 7"])]
        )

        cue.refresh_from_db()
        assert (cue.text, cue.athlete_authored) == ("RPE 7", False)
        assert [r.source_line.line for r in rows] == [2, 3]
        assert [r.set_number for r in rows] == [2, 3]
        assert not Prescription.objects.filter(text="felt heavy").exists()
        _assert_typed_shape(
            list(log.sets.select_related("source_line", "prescription"))
        )
