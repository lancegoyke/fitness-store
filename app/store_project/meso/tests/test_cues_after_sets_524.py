"""#524 backend — coach cues read-only after the sets, one session note.

Per exercise the athlete page splits the "what you did" stack: the athlete's own
lines (``sub_lines``, editable) and the coach's cues (``coach_lines``,
read-only). A changed write to a coach cue is refused (422, not 409 — see
``athlete_cell_write``), the session note is ``SessionLog.notes`` and a
notes-only post must not finish the session, and the coach sees the note on the
results page and the roster feed.
"""

import datetime
from types import SimpleNamespace

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso import views
from store_project.meso.factories import SessionLogFactory
from store_project.meso.management.commands import seed_meso_demo
from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.parsing import parse_performed
from store_project.meso.serializers import set_ordinals
from store_project.meso.tests import test_demo_typed_origin as demo_tests
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.meso.tests.test_results import results_url
from store_project.meso.tests.test_results import seed as results_seed

pytestmark = pytest.mark.django_db


def exercise_row(s, exercise=None):
    ctx = presenters.athlete_session(s.session, s.athlete)
    row = next(e for e in ctx["exercises"] if e["id"] == (exercise or s.squat).pk)
    return ctx, row


# -- 1. presenter split -----------------------------------------------------


class TestSplit:
    def test_cue_is_a_coach_line_not_a_sub_line(self):
        s = seed()
        sub_line(s.squat, "Pause at the bottom", line=1)  # coach cue
        sub_line(s.squat, "70 x 6", line=2, athlete_authored=True)
        sub_line(s.squat, "", line=3)  # blank coach line: in neither
        sub_line(s.squat, "   ", line=4, athlete_authored=True)  # blank athlete line
        ctx, row = exercise_row(s)
        assert row["coach_lines"] == [{"line": 1, "text": "Pause at the bottom"}]
        assert [(x["line"], x["text"]) for x in row["sub_lines"]] == [(2, "70 x 6")]
        assert {"warn", "warn_reason"} <= set(row["sub_lines"][0])
        payload = presenters.athlete_log_payload(ctx)
        prow = next(e for e in payload["exercises"] if e["id"] == s.squat.pk)
        assert prow["coach_lines"] == row["coach_lines"]
        assert prow["sub_lines"] == row["sub_lines"]
        assert prow["placeholder"] == row["placeholder"]
        assert prow["placeholder_reps"] == row["placeholder_reps"]

    def test_notes_ride_in_the_payload(self):
        s = seed()
        SessionLogFactory(session=s.session, athlete=s.athlete, notes="sore knee")
        ctx = presenters.athlete_session(s.session, s.athlete)
        assert ctx["notes_max"] == views.MAX_SESSION_NOTES == 2000
        payload = presenters.athlete_log_payload(ctx)
        assert payload["notes"] == "sore knee"
        assert payload["notes_max"] == 2000


# -- 2. coach lines are protected -------------------------------------------


class TestCoachLineProtected:
    def test_changed_text_is_refused_422_and_nothing_changes(self, client):
        s = seed()
        cue = sub_line(s.squat, "Pause at the bottom", line=1)
        modified = s.plan.__class__.objects.get(pk=s.plan.pk).modified
        actions = PlanAction.objects.count()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 422  # NOT 409: the client retries that forever
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "coach_line"
        assert body["error"]
        cue.refresh_from_db()
        assert cue.text == "Pause at the bottom"
        assert cue.athlete_authored is False
        assert not LoggedSet.objects.exists()
        assert not SessionLog.objects.exists()
        assert PlanAction.objects.count() == actions
        assert s.plan.__class__.objects.get(pk=s.plan.pk).modified == modified

    def test_unchanged_text_is_the_200_noop(self, client):
        s = seed()
        cue = sub_line(s.squat, "Pause at the bottom", line=1)
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "Pause at the bottom")
        assert resp.status_code == 200
        cue.refresh_from_db()
        assert cue.athlete_authored is False

    def test_blank_coach_line_is_free_to_claim(self, client):
        s = seed()
        blank = sub_line(s.squat, "", line=1)
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "70 x 6")
        assert resp.status_code == 200
        blank.refresh_from_db()
        assert blank.text == "70 x 6"
        assert blank.athlete_authored is True
        assert LoggedSet.objects.count() == 1

    def test_athletes_own_line_stays_editable(self, client):
        s = seed()
        mine = sub_line(s.squat, "70 x 6", line=1, athlete_authored=True)
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 1, "75 x 6").status_code == 200
        mine.refresh_from_db()
        assert mine.text == "75 x 6"


# -- 3. notes ---------------------------------------------------------------


class TestSessionNote:
    def test_notes_only_post_creates_a_pending_log_dated_today(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(client, s.session, {"notes": "felt heavy\n\nbut fine"})
        assert resp.status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.PENDING
        assert log.date is not None
        assert log.notes == "felt heavy\n\nbut fine"  # stored as given
        assert resp.json()["log"]["status"] == "pending"

    def test_notes_only_post_keeps_pending_status_and_date(self, client):
        s = seed()
        day_ = datetime.date(2026, 6, 20)
        SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.PENDING,
            date=day_,
        )
        client.force_login(s.athlete)
        assert log_post(client, s.session, {"notes": "hi"}).status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert (log.status, log.date, log.notes) == (
            SessionLog.Status.PENDING,
            day_,
            "hi",
        )

    def test_notes_only_post_keeps_done_status_and_date(self, client):
        s = seed()
        day_ = datetime.date(2026, 6, 20)
        SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=day_,
        )
        client.force_login(s.athlete)
        assert log_post(client, s.session, {"notes": "hi"}).status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert (log.status, log.date) == (SessionLog.Status.DONE, day_)

    def test_a_statusless_empty_post_still_finishes(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {})
        assert the_status(s) == SessionLog.Status.DONE

    def test_over_limit_is_400_and_writes_nothing(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(client, s.session, {"notes": "x" * 2001})
        assert resp.status_code == 400
        assert "2000" in resp.content.decode()
        assert not SessionLog.objects.exists()

    def test_exactly_the_limit_is_ok(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert log_post(client, s.session, {"notes": "x" * 2000}).status_code == 200
        assert len(SessionLog.objects.get().notes) == 2000


def the_status(s):
    return SessionLog.objects.get(session=s.session, athlete=s.athlete).status


# -- 4. coach sees the note -------------------------------------------------


class TestCoachSeesNote:
    def _logged(self, notes):
        s = results_seed()
        SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=datetime.date(2026, 6, 24),
            notes=notes,
        )
        return s

    def test_results_page_shows_escaped_note_and_name(self, client):
        s = self._logged("<script>alert(1)</script>\nsecond line")
        client.force_login(s.coach)
        body = client.get(results_url(s.session)).content.decode()
        assert 'data-testid="athlete-note"' in body
        assert "Note from Maya Okonkwo" in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<script>alert(1)" not in body
        assert "<br>" in body  # line break kept

    def test_no_note_no_card(self, client):
        s = self._logged("   ")
        client.force_login(s.coach)
        body = client.get(results_url(s.session)).content.decode()
        assert 'data-testid="athlete-note"' not in body

    def test_presenter_returns_the_note(self):
        s = self._logged("tight shoulder")
        assert presenters.session_results(s.session)["athlete_note"] == "tight shoulder"

    def test_roster_activity_carries_a_truncated_preview(self):
        long = "word " * 40 + "\nend"
        s = results_seed()
        SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            notes=long,
        )
        (ev,) = presenters.roster_activity(s.coach)
        assert ev["note"].endswith("…")
        assert len(ev["note"]) <= 60
        assert "\n" not in ev["note"]

    def test_roster_activity_short_and_empty_notes(self):
        s = results_seed()
        log = SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            notes="knee ok",
        )
        assert presenters.roster_activity(s.coach)[0]["note"] == "knee ok"
        log.notes = ""
        log.save(update_fields=["notes"])
        assert presenters.roster_activity(s.coach)[0]["note"] == ""

    def test_roster_template_shows_the_note_escaped(self, client):
        s = results_seed()
        SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            notes="<b>sore</b>",
        )
        client.force_login(s.coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert 'data-testid="activity-note"' in body
        assert "&lt;b&gt;sore&lt;/b&gt;" in body


# -- 6. placeholder ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "placeholder", "reps"),
    [
        ("3x5 @ 225", "225 x 5", ""),
        ("3 x 5 @ 102.5", "102.5 x 5", ""),
        ("3x5 @ 225.0", "225 x 5", ""),
        ("3x5 @ 225 lb", "225 x 5", ""),
        ("4 x 6, RPE 7, 72%", "", "6"),
        ("5 x 5 @ 80%", "", "5"),
        ("3 x 8-10", "", ""),
        ("AMRAP", "", ""),
        ("3x5 @ BW", "", ""),
        ("60s", "", ""),
        ("3x5", "", ""),
        ("", "", ""),
    ],
)
def test_line_placeholder(text, placeholder, reps):
    got = presenters._line_placeholder(SimpleNamespace(text=text))
    assert got == (placeholder, reps)
    if placeholder:
        parsed = parse_performed(placeholder)
        assert parsed["kind"] == "set"
        assert parsed["reps"] == int(placeholder.split(" x ")[1])
        assert str(parsed["load"]) == placeholder.split(" x ")[0]


def test_placeholder_rides_the_exercise_row():
    s = seed()
    s.squat.text = "3x5 @ 225"
    s.squat.save(update_fields=["text"])
    _, row = exercise_row(s)
    assert (row["placeholder"], row["placeholder_reps"]) == ("225 x 5", "")


# -- 7. demo ----------------------------------------------------------------


class TestDemoNotes:
    def test_sandbox_demo_log_has_a_note(self):
        from store_project.meso import demo

        coach = demo_tests._demo_coach()
        demo.load_log(coach)
        notes = list(
            SessionLog.objects.filter(
                athlete__in=demo._demo_athletes(coach)
            ).values_list("notes", flat=True)
        )
        assert notes
        assert all(n.strip() for n in notes)

    def test_seeded_history_logs_carry_notes(self):
        from django.core.management import call_command

        call_command("seed_meso_demo", coach_email=demo_tests.COACH_EMAIL)
        assert SessionLog.objects.filter(notes=seed_meso_demo.SAMPLE_LOG_NOTE).exists()
        assert SessionLog.objects.exclude(notes="").count() > 1


# -- 8. set ordinals --------------------------------------------------------


def test_ordinals_ignore_where_the_coach_cue_sits(client):
    s = seed()
    sub_line(s.squat, "Pause at the bottom", line=1)  # coach cue on line 1
    client.force_login(s.athlete)
    for line in (2, 3, 4):
        assert write_cell(client, s.session, s.squat, line, "70 x 6").status_code == 200
    sets = list(LoggedSet.objects.order_by("set_number"))
    assert [x.set_number for x in sets] == [2, 3, 4]  # stored as the cell line
    ordinals = set_ordinals(sets)
    assert [ordinals[x.pk] for x in sets] == [1, 2, 3]
    assert Prescription.objects.filter(athlete_authored=False, line=1).count() == 1
