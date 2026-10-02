"""A coach undo that restores a rewritten line's text shows the set once (#561).

The sequence, end to end through the real views:

1. the athlete types ``225 x 5`` on sub-line 1 (parsed row A, ``source_line``
   = that cell);
2. the coach rewrites that line (``cell_line_write``) — the athlete's page now
   shows the coach's text AND A as a read-only history row;
3. the coach undoes the rewrite, and sub-line 1 reads ``225 x 5`` again.

The data is right either way — one ``LoggedSet``. This pins the PAGE: the line
shows the performance, the read-only history does not list it too, and the line
is not tinted "not logged as a set". Nothing here writes athlete data on the
coach's undo, and nothing writes on a GET.
"""

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import legacy_reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def _undo(client, s, times=1):
    url = reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk})
    for _ in range(times):
        assert client.post(url, content_type="application/json").status_code == 200


def _squat_view(s):
    ctx = presenters.athlete_session(s.session, s.athlete)
    return next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)


def _squat_rows(s):
    return list(
        LoggedSet.objects.filter(
            session_log__session=s.session, prescription=s.squat
        ).order_by("set_number")
    )


def _rewrite_then_undo(client, s):
    """Steps 1-3: type, rewrite, then the coach's undo."""
    client.force_login(s.athlete)
    write_cell(client, s.session, s.squat, 1, "225 x 5")
    cell = sub_cell(s.squat, 1)

    client.force_login(s.coach)
    legacy_reclaim(s, text="brace harder")

    client.force_login(s.athlete)
    squat = _squat_view(s)
    assert [line["text"] for line in squat["coach_lines"]][:1] == ["brace harder"]
    assert any("225" in r["label"] for r in squat["logged_readonly"])

    client.force_login(s.coach)
    _undo(client, s)

    cell.refresh_from_db()
    assert cell.text == "225 x 5", "the undo should put the athlete's text back"
    assert cell.athlete_authored is True, "handed back to the athlete (#703)"
    client.force_login(s.athlete)
    return cell


class TestTheUndoneRewriteShowsTheSetOnce:
    def test_the_line_shows_it_and_the_history_does_not(self, client):
        """The issue: ``225 x 5`` on the line AND the same set listed again."""
        s = seed()
        cell = _rewrite_then_undo(client, s)

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.source_line_id, r.load, r.reps) for r in rows]
        assert rows[0].source_line_id == cell.pk

        squat = _squat_view(s)
        # The restore hands the cell back to the athlete (#703), so the line
        # is their own editable sub-line, once, and not a coach cue.
        assert [line["text"] for line in squat["sub_lines"]] == ["225 x 5"]
        assert squat["coach_lines"] == []
        assert squat["logged_readonly"] == [], (
            "the line already shows this performance, so the read-only history "
            f"must not list it too: {squat['logged_readonly']}"
        )

    def test_the_line_is_not_tinted(self, client):
        """``sub_line_warn_reason`` must not claim the line logged nothing."""
        s = seed()
        _rewrite_then_undo(client, s)

        # Athlete-owned again since the restore (#703): an editable line that
        # shows the set, so it is not tinted.
        squat = _squat_view(s)
        assert squat["coach_lines"] == []
        assert [line["text"] for line in squat["sub_lines"]] == ["225 x 5"]
        assert squat["sub_lines"][0]["warn"] is False

    def test_the_cell_write_response_agrees_with_the_reload(self, client):
        """A blur on the restored line reports no tint, as the page reload does."""
        s = seed()
        _rewrite_then_undo(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn"] is False


class TestTheUndoWritesNoAthleteData:
    def test_the_set_is_untouched_by_the_undo(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        legacy_reclaim(s, text="brace harder")
        fields = (
            "pk",
            "session_log_id",
            "prescription_id",
            "source_line_id",
            "set_number",
            "reps",
            "load",
            "rpe",
        )
        before = sorted(LoggedSet.objects.values_list(*fields))

        _undo(client, s)

        after = sorted(LoggedSet.objects.values_list(*fields))
        assert after == before, "a coach undo must never write athlete data"

    def test_rendering_the_page_writes_nothing(self, client):
        s = seed()
        _rewrite_then_undo(client, s)
        fields = ("pk", "source_line_id", "set_number", "load", "reps")
        before = sorted(LoggedSet.objects.values_list(*fields))

        _squat_view(s)
        assert client.get(
            reverse("meso:athlete_session", kwargs={"pk": s.session.pk})
        ).status_code in (200, 302)

        after = sorted(LoggedSet.objects.values_list(*fields))
        assert after == before, "a GET must not re-link anything"
