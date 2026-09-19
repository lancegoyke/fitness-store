"""Restoring a reclaimed sub-line after "Log session" (#541).

The sequence, end to end through the real views:

1. the athlete types ``225 x 5`` on sub-line 1 (parsed row A, ``source_line``
   = that cell);
2. the coach rewrites the line (``cell_line_write``), which reclaims it — A is
   no longer shown by its line, so the structured logger renders it;
3. the athlete taps "Log session" with that row unchanged — the save replaces A
   with a source-less structured copy S;
4. the athlete types ``225 x 5`` back onto sub-line 1.

One performance, so the session must end with one ``LoggedSet``.
"""

import pytest

from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.serializers import serialize_session_log
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def _log_session_as_rendered(client, s, status="done"):
    """Tap "Log session" posting exactly the rows the logger re-hydrates from."""
    rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
    return log_post(
        client,
        s.session,
        {
            "status": status,
            "sets": [
                {
                    "prescription": row["prescription"],
                    "set_number": row["set_number"],
                    "reps": row["reps"],
                    "load": row["load"],
                    "rpe": row["rpe"],
                }
                for row in rendered
            ],
        },
    )


def _squat_rows(s):
    return list(
        LoggedSet.objects.filter(
            session_log__session=s.session, prescription=s.squat
        ).order_by("set_number")
    )


def _reclaim_then_log(client, s):
    client.force_login(s.athlete)
    write_cell(client, s.session, s.squat, 1, "225 x 5")

    client.force_login(s.coach)
    assert reclaim(client, s, text="brace harder").status_code == 200

    client.force_login(s.athlete)
    # The page the athlete taps "Log session" on shows the reclaimed row in the
    # structured logger AND the coach's text on sub-line 1.
    ctx = presenters.athlete_session(s.session, s.athlete)
    squat = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
    assert any(r["load"] == "225" for r in squat["set_rows"])
    assert [line["text"] for line in squat["sub_lines"]][:1] == ["brace harder"]

    assert _log_session_as_rendered(client, s).status_code == 200


class TestRestoringAfterLogSessionKeepsOneRow:
    def test_retyping_the_set_leaves_one_row(self, client):
        s = seed()
        _reclaim_then_log(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, (
            "one performance is logged twice: "
            f"{[(r.pk, r.source_line_id, r.set_number, r.load, r.reps) for r in rows]}"
        )
        assert (rows[0].load, rows[0].reps) == ("225", "5")
