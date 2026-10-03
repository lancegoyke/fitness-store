"""#578 stage 4 PR 1 — the Set-row logger is retired; typed lines are the only way to log.

The athlete types a line ("225 x 5, RPE 8") under "what you did" and it saves on
blur through ``athlete_cell_write`` -> ``_upsert_parsed_set``. ``athlete_log_session``
now writes status/date/notes only: ``sets`` in a body is ignored (an installed PWA
can still post it), and both the athlete's header and the coach's results read one
shared count (``presenters.set_progress``).
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import cell_post
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db

LEGACY_EVENT = "legacy_sets_ignored"


def seed_four_sets():
    """One exercise, "4 x 5 @ 100" (the other row is skipped, so it is not trainable)."""
    s = seed()
    s.squat.text = "4 x 5 @ 100"
    s.squat.save(update_fields=["text"])
    s.rdl.skipped = True
    s.rdl.save(update_fields=["skipped"])
    return s


def page_url(session):
    return reverse("meso:athlete_session", kwargs={"pk": session.pk})


def get_page(client, session):
    resp = client.get(page_url(session))
    assert resp.status_code == 200
    return resp


def type_sets(client, s, n):
    for line in range(1, n + 1):
        resp = write_cell(client, s.session, s.squat, line, "100 x 5")
        assert resp.status_code == 200


def counts(progress):
    """A progress payload without its ``as_of`` read stamp."""
    return {k: v for k, v in progress.items() if k != "as_of"}


def events(name):
    return list(Event.objects.filter(name=name).order_by("id"))


def legacy_structured_set(s, log, *, set_number=9, reps="6", load="70", rpe="7"):
    """What the retired logger wrote: no source_line, filed under the line-0 cell."""
    return LoggedSet.objects.create(
        session_log=log,
        prescription=s.squat,
        exercise_slot=s.squat.exercise_slot,
        set_number=set_number,
        reps=reps,
        load=load,
        rpe=rpe,
        source_line=None,
    )


# -- the page ---------------------------------------------------------------


class TestPage:
    def test_session_page_has_no_set_rows_or_two_ways_card(self, client):
        s = seed()
        client.force_login(s.athlete)  # a first-time logger: no done log yet
        resp = get_page(client, s.session)
        html = resp.content.decode()

        assert "Two ways to log a set" not in html
        assert "meso-set-row" not in html
        assert 'data-testid="set-toggle"' not in html
        assert 'data-testid="session-save"' not in html
        assert "Save progress" not in html
        for exercise in resp.context["log_data"]["exercises"]:
            assert "set_rows" not in exercise

    def test_page_has_one_finish_button(self, client):
        s = seed()
        client.force_login(s.athlete)
        html = get_page(client, s.session).content.decode()

        assert 'data-testid="session-finish"' in html
        assert "Finish session" in html
        assert 'data-testid="session-log"' not in html

    def test_header_first_paint_shows_progress(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        type_sets(client, s, 3)

        resp = get_page(client, s.session)

        assert "3 of 4 sets logged" in resp.content.decode()
        assert counts(resp.context["log_data"]["progress"]) == {
            "logged": 3,
            "prescribed": 4,
        }

    def test_log_data_carries_pad_lines(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        resp = get_page(client, s.session)

        (exercise,) = resp.context["log_data"]["exercises"]
        assert exercise["pad_lines"] == 4
        assert exercise["logged_readonly"] == []


# -- the finish endpoint ----------------------------------------------------


class TestFinish:
    def test_finish_marks_done_stamps_date_returns_progress(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        type_sets(client, s, 3)

        resp = log_post(client, s.session, {"status": "done"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert counts(body["progress"]) == {"logged": 3, "prescribed": 4}
        assert "new_records" not in body
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.DONE
        assert log.date == timezone.localdate()
        assert body["log"] == {
            "id": log.pk,
            "status": "done",
            "date": log.date.isoformat(),
            "notes": "",
        }
        assert len(events("session_completed")) == 1
        # A second Finish adds no second completion.
        log_post(client, s.session, {"status": "done"})
        assert len(events("session_completed")) == 1

    def test_absent_notes_are_left_untouched(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        type_sets(client, s, 1)
        log = the_log(s.session, s.athlete)
        log.notes = "knee felt fine"
        log.save(update_fields=["notes"])

        resp = log_post(client, s.session, {"status": "done"})

        assert resp.status_code == 200
        log.refresh_from_db()
        assert log.notes == "knee felt fine"
        assert resp.json()["log"]["notes"] == "knee felt fine"

    def test_present_notes_save_without_finishing(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        type_sets(client, s, 1)

        resp = log_post(client, s.session, {"status": "pending", "notes": "x"})

        assert resp.status_code == 200
        log = the_log(s.session, s.athlete)
        assert log.notes == "x"
        assert log.status == SessionLog.Status.PENDING

    def test_non_string_notes_are_still_a_400(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        resp = log_post(client, s.session, {"notes": 5})

        assert resp.status_code == 400
        assert not SessionLog.objects.exists()

    def test_legacy_sets_body_is_ignored_with_200(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        body = {
            "status": "done",
            "notes": "from an old tab",
            "sets": [
                {
                    "prescription": s.squat.pk,
                    "set_number": 1,
                    "reps": "5",
                    "load": "100",
                    "rpe": "8",
                }
            ],
        }

        resp = log_post(client, s.session, body)

        assert resp.status_code == 200
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.DONE
        assert log.notes == "from an old tab"
        assert LoggedSet.objects.count() == 0
        (event,) = events(LEGACY_EVENT)
        assert event.props == {"count": 1}
        assert event.actor == s.athlete
        assert event.subject_id == str(log.pk)

    def test_malformed_legacy_sets_is_ignored_not_400(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        resp = log_post(client, s.session, {"status": "done", "sets": "junk"})

        assert resp.status_code == 200
        assert LoggedSet.objects.count() == 0
        (event,) = events(LEGACY_EVENT)
        assert event.props == {"count": 0}

    def test_no_sets_key_records_no_legacy_event(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        log_post(client, s.session, {"status": "done"})

        assert events(LEGACY_EVENT) == []


# -- one count, two screens -------------------------------------------------


def _typed_only(client, s):
    type_sets(client, s, 3)
    log_post(client, s.session, {"status": "done"})


def _mixed(client, s):
    type_sets(client, s, 2)
    log_post(client, s.session, {"status": "done"})
    legacy_structured_set(s, the_log(s.session, s.athlete))


def _empty(client, s):
    log_post(client, s.session, {"status": "done"})


class TestHeaderMatchesResults:
    @pytest.mark.parametrize(
        "build, expected_logged",
        [(_typed_only, 3), (_mixed, 3), (_empty, 0)],
        ids=["typed-only", "mixed", "empty"],
    )
    def test_header_count_equals_results_count(self, client, build, expected_logged):
        s = seed_four_sets()
        client.force_login(s.athlete)
        build(client, s)

        athlete_ctx = presenters.athlete_session(s.session, s.athlete)
        results = presenters.session_results(s.session)
        summary = results["summary"]

        assert summary["logged_sets"] == expected_logged
        assert athlete_ctx["progress"] == {
            "logged": summary["logged_sets"],
            "prescribed": summary["prescribed_sets"],
        }
        prescribed = summary["prescribed_sets"]
        assert prescribed == 4
        expected_completion = (
            min(round(100 * summary["logged_sets"] / prescribed), 100)
            if prescribed
            else 0
        )
        assert summary["completion"] == expected_completion
        assert athlete_ctx["progress_label"] == summary["progress_label"]

    def test_results_tile_shows_the_label(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        _typed_only(client, s)
        client.force_login(s.coach)

        resp = client.get(
            reverse("meso:results_session", kwargs={"session_id": s.session.pk})
        )

        assert resp.status_code == 200
        assert "3 of 4 sets logged" in resp.content.decode()

    def test_label_wording(self):
        assert presenters.progress_label(3, 4) == "3 of 4 sets logged"
        assert presenters.progress_label(1, 1) == "1 of 1 set logged"
        assert presenters.progress_label(0, 0) == "0 sets logged"
        assert presenters.progress_label(1, 0) == "1 set logged"
        assert presenters.progress_label(2, 0) == "2 sets logged"


# -- legacy history ----------------------------------------------------------


class TestLegacyHistory:
    def test_legacy_structured_set_displays_read_only(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")  # still shown by its line
        write_cell(client, s.session, s.squat, 2, "225 x 5")
        log = the_log(s.session, s.athlete)
        legacy = legacy_structured_set(s, log, set_number=9)
        # The coach rewrites the second line: its typed set is no longer shown there.
        overwritten = Prescription.objects.get(
            exercise_slot=s.squat.exercise_slot, week=s.squat.week, line=2
        )
        overwritten.text = "Pause 2s at the bottom"
        overwritten.athlete_authored = False
        overwritten.save(update_fields=["text", "athlete_authored"])
        log_post(client, s.session, {"status": "done"})

        ctx = presenters.athlete_session(s.session, s.athlete)
        (exercise,) = ctx["exercises"]

        labels = [row["label"] for row in exercise["logged_readonly"]]
        assert len(labels) == 2  # the legacy set once, the overwritten typed set once
        assert [row["set_number"] for row in exercise["logged_readonly"]] == [2, 9]
        assert labels[0].startswith("Set 2 · 225 ") and labels[0].endswith(" × 5")
        # "Set N" is the ordinal among the exercise's logged sets (#691): the
        # stored numbers 1, 2, 9 read as sets 1, 2, 3.
        assert labels[1] == f"Set 3 · 70 {legacy.unit} × 6 · RPE 7"
        # The typed set its own line still shows is NOT listed.
        assert not any("100" in label for label in labels)
        # The coach's results still count all three.
        summary = presenters.session_results(s.session)["summary"]
        assert summary["logged_sets"] == 3
        (row,) = presenters.session_results(s.session)["rows"]
        assert row["note"] == "3/4 sets logged"
        # And the read-only entries ride the payload the page hydrates from.
        payload = presenters.athlete_log_payload(ctx)
        assert payload["exercises"][0]["logged_readonly"] == exercise["logged_readonly"]
        assert "set_rows" not in payload["exercises"][0]

    def test_label_forms(self):
        s = seed_four_sets()
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        bw = legacy_structured_set(s, log, set_number=1, reps="8", load="BW", rpe="")
        reps_only = legacy_structured_set(
            s, log, set_number=2, reps="10", load="", rpe=""
        )
        assert presenters._logged_set_label(bw, "kg", 1) == "Set 1 · BW × 8"
        assert presenters._logged_set_label(reps_only, "kg", 2) == "Set 2 · 10 reps"


# -- cell write + PWA ---------------------------------------------------------


class TestCellWriteAndPwa:
    def test_cell_write_response_carries_progress(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        first = write_cell(client, s.session, s.squat, 1, "100 x 5")
        second = write_cell(client, s.session, s.squat, 2, "100 x 5")

        assert counts(first.json()["progress"]) == {"logged": 1, "prescribed": 4}
        assert counts(second.json()["progress"]) == {"logged": 2, "prescribed": 4}
        # A later read carries a later as_of, so the client can drop a
        # response that lands after a newer one (two lines' saves overlap).
        assert second.json()["progress"]["as_of"] > first.json()["progress"]["as_of"]

    def test_non_set_text_reports_unchanged_progress(self, client):
        s = seed_four_sets()
        client.force_login(s.athlete)

        resp = cell_post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "felt heavy"},
        )

        assert counts(resp.json()["progress"]) == {"logged": 0, "prescribed": 4}

    def test_pwa_cache_version_bumped(self):
        assert views.PWA_CACHE_VERSION == "meso-pwa-v17"
