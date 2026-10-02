"""A coach undo that restores a reclaimed line's text over a legacy copy (#561).

The sequence, end to end through the real views:

1. the athlete types ``225 x 5`` on sub-line 1 (parsed row A);
2. the coach rewrites that line (``cell_line_write``), which reclaims it — the
   athlete's page now shows the coach's text AND A as a filled Set row;
3. A is replaced by a source-less copy S carrying ``reclaimed_line`` = sub-line
   1 (#541) -- what the retired "Log session" logger wrote; the tests build it
   with the ORM (``_reclaim_then_log``);
4. the coach undoes the rewrite, and sub-line 1 reads ``225 x 5`` again.

The data is right either way — one ``LoggedSet``. This pins the PAGE: the line
shows the performance, the read-only history does not list it too, and the line
is not tinted "not logged as a set". Nothing here writes athlete data on the
coach's undo, and nothing writes on a GET.
"""

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell
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


def _undo_the_reclaim(client, s):
    """Steps 1-4: type, reclaim, legacy copy, then the coach's undo."""
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
    def test_the_line_shows_it_and_the_history_does_not(self, client):
        """The issue: ``225 x 5`` on the line AND the same set listed again."""
        s = seed()
        cell = _undo_the_reclaim(client, s)

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.source_line_id, r.load, r.reps) for r in rows]
        assert rows[0].source_line_id is None
        assert rows[0].reclaimed_line_id == cell.pk

        squat = _squat_view(s)
        assert [line["text"] for line in squat["sub_lines"]][:1] == ["225 x 5"]
        assert squat["logged_readonly"] == [], (
            "the line already shows this performance, so the read-only history "
            f"must not list it too: {squat['logged_readonly']}"
        )

    def test_the_line_is_not_tinted(self, client):
        """``sub_line_warn_reason`` claimed the line logged nothing."""
        s = seed()
        _undo_the_reclaim(client, s)

        squat = _squat_view(s)
        assert [line["warn"] for line in squat["sub_lines"]][:1] == [False], (
            "the line IS backed by a logged set — the copy the reclaim left "
            "behind, which its text is showing again"
        )

    def test_the_cell_write_response_agrees_with_the_reload(self, client):
        """A blur on the restored line reports no tint, as the page reload does."""
        s = seed()
        _undo_the_reclaim(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn"] is False


class TestOnlyOneRowPerLineIsHidden:
    """A line shows ONE performance, so its own parsed row outranks a copy.

    #541 leaves the copy alone when the athlete corrects a line that was
    showing a set of its own, and both rows can end up with the copy's values.
    The line displays one of them; the other is a read-only history row.
    """

    def test_a_correction_matching_the_copy_leaves_the_copy_visible(self, client):
        s = seed()
        _reclaim_then_log(client, s)

        assert write_cell(client, s.session, s.squat, 1, "230 x 3").status_code == 200
        assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 2, [(r.source_line_id, r.load, r.reps) for r in rows]

        squat = _squat_view(s)
        assert len(squat["logged_readonly"]) == 1, (
            "the line shows its own parsed row; the legacy copy is a "
            f"separate performance and stays in the read-only history: "
            f"{squat['logged_readonly']}"
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
