"""A coach undo that restores a reclaimed line's text after "Log session" (#561).

The sequence, end to end through the real views:

1. the athlete types ``225 x 5`` on sub-line 1 (parsed row A);
2. the coach rewrites that line (``cell_line_write``), which reclaims it — the
   athlete's page now shows the coach's text AND A as a filled Set row;
3. the athlete taps "Log session", so A is replaced by a source-less copy S
   carrying ``reclaimed_line`` = sub-line 1 (#541);
4. the coach undoes the rewrite, and sub-line 1 reads ``225 x 5`` again.

The data is right either way — one ``LoggedSet``. This pins the PAGE: the line
shows the performance, the structured logger does not show it too, and the line
is not tinted "not logged as a set". Nothing here writes athlete data on the
coach's undo, and nothing writes on a GET.
"""

import pytest
from django.urls import reverse

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.serializers import serialize_session_log
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.meso.tests.test_reclaim_restore_after_log import (
    _log_session_as_rendered,
)
from store_project.meso.tests.test_reclaim_restore_after_log import _reclaim_then_log
from store_project.meso.tests.test_reclaim_restore_after_log import _squat_rows

pytestmark = pytest.mark.django_db


def _undo(client, s, times=1):
    url = reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk})
    for _ in range(times):
        assert client.post(url, content_type="application/json").status_code == 200


def _squat_view(s):
    ctx = presenters.athlete_session(s.session, s.athlete)
    return next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)


def _rendered_sets(s):
    """The payload the athlete's page would post from what it currently shows."""
    return [
        {
            "prescription": row["prescription"],
            "set_number": row["set_number"],
            "reps": row["reps"],
            "load": row["load"],
            "rpe": row["rpe"],
        }
        for row in serialize_session_log(the_log(s.session, s.athlete))["sets"]
    ]


def _undo_the_reclaim(client, s):
    """Steps 1-4: type, reclaim, Log session, then the coach's undo."""
    _reclaim_then_log(client, s)
    cell = sub_cell(s.squat, 1)

    client.force_login(s.coach)
    _undo(client, s)

    cell.refresh_from_db()
    assert cell.text == "225 x 5", "the undo should put the athlete's text back"
    assert cell.athlete_authored is False, "restored as a coach-owned cell"
    client.force_login(s.athlete)
    return cell


class TestTheUndoneReclaimShowsTheSetOnce:
    def test_the_line_shows_it_and_the_logger_does_not(self, client):
        """The issue: ``225 x 5`` on the line AND the same set in the logger."""
        s = seed()
        cell = _undo_the_reclaim(client, s)

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.source_line_id, r.load, r.reps) for r in rows]
        assert rows[0].source_line_id is None
        assert rows[0].reclaimed_line_id == cell.pk

        squat = _squat_view(s)
        assert [line["text"] for line in squat["sub_lines"]][:1] == ["225 x 5"]
        assert all(r["load"] == "" and r["reps"] == "" for r in squat["set_rows"]), (
            "the line already shows this performance, so the structured logger "
            f"must not show it too: {squat['set_rows']}"
        )
        assert all(r["done"] is False for r in squat["set_rows"])

    def test_the_line_is_not_tinted(self, client):
        """``sub_line_warn_reason`` claimed the line logged nothing."""
        s = seed()
        _undo_the_reclaim(client, s)

        squat = _squat_view(s)
        assert [line["warn"] for line in squat["sub_lines"]][:1] == [False], (
            "the line IS backed by a logged set — the copy the reclaim left "
            "behind, which its text is showing again"
        )

    def test_the_log_endpoints_view_of_itself_agrees(self, client):
        """``serialize_session_log`` feeds the client's re-hydrate; same rule."""
        s = seed()
        _undo_the_reclaim(client, s)

        assert serialize_session_log(the_log(s.session, s.athlete))["sets"] == []

    def test_the_cell_write_response_agrees_with_the_reload(self, client):
        """A blur on the restored line reports no tint, as the page reload does."""
        s = seed()
        _undo_the_reclaim(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn"] is False


class TestLogSessionFromAStalePageKeepsOneRow:
    """The replace-delete keys on the same predicate, so check the save too.

    A page loaded before the undo still shows the copy as a filled Set row and
    re-posts it. That row is now hidden, so it is no longer replaceable — and
    creating the posted row on top of it would log one performance twice.
    """

    def test_reposting_the_stale_set_row_does_not_duplicate(self, client):
        s = seed()
        _reclaim_then_log(client, s)
        stale = _rendered_sets(s)
        assert len(stale) == 1, "the page under test must be showing the Set row"
        copy = _squat_rows(s)[0]

        client.force_login(s.coach)
        _undo(client, s)

        client.force_login(s.athlete)
        resp = log_post(client, s.session, {"status": "done", "sets": stale})
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, (
            "one performance, two rows: "
            f"{[(r.pk, r.source_line_id, r.set_number, r.load, r.reps) for r in rows]}"
        )
        assert rows[0].pk == copy.pk, "the surviving row is the copy, untouched"
        assert resp.json()["log"]["sets"] == []

    def test_the_stale_repost_logs_no_new_set_event(self, client):
        s = seed()
        _reclaim_then_log(client, s)
        stale = _rendered_sets(s)

        client.force_login(s.coach)
        _undo(client, s)

        client.force_login(s.athlete)
        assert (
            log_post(client, s.session, {"status": "done", "sets": stale}).status_code
            == 200
        )

        assert Event.objects.filter(name=EventName.SET_LOGGED).count() == 1

    def test_a_fresh_page_saves_without_touching_the_copy(self, client):
        """The ordinary case: the page reloaded after the undo posts no sets."""
        s = seed()
        _undo_the_reclaim(client, s)
        copy = _squat_rows(s)[0]

        resp = log_post(
            client, s.session, {"status": "done", "sets": _rendered_sets(s)}
        )
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1 and rows[0].pk == copy.pk, (
            "a save that posts nothing must not delete a set the logger cannot "
            "see — the line is showing it"
        )


class TestASecondSetOnTheRestoredLineStaysSeparate:
    """A hidden copy still owns its set number, so the logger must move off it.

    The copy is numbered 1 and is now invisible, so the athlete's empty Set row
    1 is the obvious place to log the next set. Two rows sharing
    ``(prescription, set_number)`` collapse in ``athlete_session``'s dict, and
    one later save can then delete both while reposting one.
    """

    def test_logging_set_one_after_the_undo_renumbers_the_copy(self, client):
        s = seed()
        _undo_the_reclaim(client, s)

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "3",
                        "load": "315",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert sorted((r.load, r.reps) for r in rows) == [("225", "5"), ("315", "3")]
        assert len({r.set_number for r in rows}) == 2, (
            "the hidden copy kept the set number the client just posted: "
            f"{[(r.set_number, r.load, r.reps) for r in rows]}"
        )

    def test_a_later_reclaim_shows_both_and_a_save_keeps_both(self, client):
        s = seed()
        _undo_the_reclaim(client, s)
        log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "3",
                        "load": "315",
                        "rpe": "",
                    }
                ],
            },
        )

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        squat = _squat_view(s)
        shown = sorted((r["load"], r["reps"]) for r in squat["set_rows"] if r["done"])
        assert shown == [("225", "5"), ("315", "3")], (
            f"both performances belong in the logger now: {squat['set_rows']}"
        )

        assert _log_session_as_rendered(client, s).status_code == 200
        rows = _squat_rows(s)
        assert sorted((r.load, r.reps) for r in rows) == [("225", "5"), ("315", "3")]


class TestOnlyOneRowPerLineIsHidden:
    """A line shows ONE performance, so its own parsed row outranks a copy.

    #541 leaves the copy alone when the athlete corrects a line that was
    showing a set of its own, and both rows can end up with the copy's values.
    The line displays one of them; the other is a real logger row.
    """

    def test_a_correction_matching_the_copy_leaves_the_copy_visible(self, client):
        s = seed()
        _reclaim_then_log(client, s)

        assert write_cell(client, s.session, s.squat, 1, "230 x 3").status_code == 200
        assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 2, [(r.source_line_id, r.load, r.reps) for r in rows]

        squat = _squat_view(s)
        shown = [(r["load"], r["reps"]) for r in squat["set_rows"] if r["done"]]
        assert shown == [("225", "5")], (
            "the line shows its own parsed row; the structured copy is a "
            f"separate performance and stays in the logger: {squat['set_rows']}"
        )
        assert [line["warn"] for line in squat["sub_lines"]][:1] == [False]


class TestTheUndoWritesNoAthleteData:
    def test_the_copy_is_untouched_by_the_undo(self, client):
        s = seed()
        _reclaim_then_log(client, s)
        before = LoggedSet.objects.values_list(
            "pk",
            "session_log_id",
            "prescription_id",
            "source_line_id",
            "reclaimed_line_id",
            "set_number",
            "reps",
            "load",
            "rpe",
        )
        before = sorted(before)

        client.force_login(s.coach)
        _undo(client, s)

        after = sorted(
            LoggedSet.objects.values_list(
                "pk",
                "session_log_id",
                "prescription_id",
                "source_line_id",
                "reclaimed_line_id",
                "set_number",
                "reps",
                "load",
                "rpe",
            )
        )
        assert after == before, "a coach undo must never write athlete data"

    def test_rendering_the_page_writes_nothing(self, client):
        s = seed()
        _undo_the_reclaim(client, s)
        before = sorted(
            LoggedSet.objects.values_list(
                "pk", "source_line_id", "reclaimed_line_id", "set_number"
            )
        )

        _squat_view(s)
        assert client.get(
            reverse("meso:athlete_session", kwargs={"pk": s.session.pk})
        ).status_code in (200, 302)

        after = sorted(
            LoggedSet.objects.values_list(
                "pk", "source_line_id", "reclaimed_line_id", "set_number"
            )
        )
        assert after == before, "a GET must not re-link anything"
