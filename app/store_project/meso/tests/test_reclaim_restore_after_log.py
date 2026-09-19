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

    def test_the_row_is_relinked_and_hidden_with_no_toast(self, client):
        """The restore reuses ``S`` in place — same row, re-linked, not new.

        Matches ``TestRestoringAReclaimedLineDoesNotDuplicate`` (no reclaim in
        between): ``reclaimed_line`` clears back to ``None`` the moment
        ``source_line`` is set, the row goes back to being hidden by its own
        sub-line's text, and there is nothing new to celebrate.
        """
        s = seed()
        _reclaim_then_log(client, s)
        cell = sub_cell(s.squat, 1)

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        body = resp.json()
        assert body["new_records"] == []
        assert body["cell"]["warn"] is False

        rows = _squat_rows(s)
        assert len(rows) == 1
        row = rows[0]
        assert row.source_line_id == cell.pk
        assert row.reclaimed_line_id is None

        ctx = presenters.athlete_session(s.session, s.athlete)
        squat = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert all(r["load"] == "" for r in squat["set_rows"]), (
            "the restored row must be hidden by sub-line 1's own text again, "
            "not double-displayed as a structured row too"
        )
        assert serialize_session_log(the_log(s.session, s.athlete))["sets"] == []


class TestTheLinkSurvivesRepeatedLogSessions:
    def test_two_saves_before_the_retype_still_leave_one_row(self, client):
        """The link is carried forward by ``athlete_log_session`` itself.

        A second "Log session"/"Save progress" replaces ``S`` with a fresh
        source-less copy before the athlete ever gets around to retyping the
        sub-line — the ``reclaimed_line`` carry has to survive that resave, not
        just the first one, or a tab left open across two saves loses the link
        exactly like #541 did.
        """
        s = seed()
        _reclaim_then_log(client, s)

        # A "Save progress" ("pending") lands on top of the already-DONE log —
        # status is sticky, but the sets are replaced exactly like a done save.
        resp = _log_session_as_rendered(client, s, status="pending")
        assert resp.status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.load, r.reps) for r in rows]
        assert (rows[0].load, rows[0].reps) == ("225", "5")


class TestADifferentValueDoesNotClaimTheLink:
    def test_retyping_a_different_value_adds_a_second_row(self, client):
        """Only an identical restore reuses the row — the boundary #541 needs.

        A genuinely different performance on the same sub-line must not be
        swallowed by a stale link: ``S`` (225 x 5) survives untouched and the
        new text mints its own parsed row.
        """
        s = seed()
        _reclaim_then_log(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "230 x 3")
        assert resp.status_code == 200

        pairs = sorted((r.load, r.reps) for r in _squat_rows(s))
        assert pairs == [("225", "5"), ("230", "3")]


class TestEditingTheStructuredCopyDropsTheLink:
    def test_editing_then_retyping_the_original_adds_a_second_row(self, client):
        """A repost with new values is an edit, not a restore — no carry.

        Editing ``S`` inside the structured logger (still slot 1, new values)
        must not leave the edited row still answering to sub-line 1's original
        text: the carry dies with the edit, so retyping the original text
        mints a fresh parsed row alongside it instead of silently rewriting
        the athlete's edit.
        """
        s = seed()
        _reclaim_then_log(client, s)

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "225",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        pairs = sorted((r.load, r.reps) for r in _squat_rows(s))
        assert pairs == [("225", "5"), ("225", "6")]


class TestASameValuedStructuredSetIsNotMergedByDefault:
    def test_no_reclaim_at_all_still_gives_two_rows(self, client):
        """Pin the normal case: no reclaim means no link, so no merge at all.

        A structured set and a freeform-typed set that happen to share values
        are an ordinary thing (225 x 5 twice) — matching by value alone would
        wrongly fold them into one performance.
        """
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        assert len(_squat_rows(s)) == 2, (
            "a same-valued structured set must not be merged with an "
            "unrelated typed one — only a genuine restore reuses a row"
        )


class TestClearingTheStructuredCopyStillDeletesIt:
    def test_posting_no_sets_deletes_the_carried_copy(self, client):
        """Normal logger behavior is unaffected by the new carry machinery."""
        s = seed()
        _reclaim_then_log(client, s)

        resp = log_post(client, s.session, {"status": "pending", "sets": []})
        assert resp.status_code == 200

        assert _squat_rows(s) == []


class TestExactlyOneSetLoggedEventAcrossTheSequence:
    def test_the_whole_541_sequence_logs_one_event(self, client):
        """Type, reclaim, Log session, restore — one performance, one event.

        The Log session save nets zero (a resave replaces its own row) and the
        restore reuses ``S`` rather than creating, so ``set_logged`` only ever
        fires once, at the original typed blur.
        """
        s = seed()
        _reclaim_then_log(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = list(Event.objects.filter(name=EventName.SET_LOGGED).order_by("id"))
        assert len(rows) == 1


class TestASecondReclaimCycleStillEndsWithOneRow:
    def test_reclaim_log_restore_twice(self, client):
        """The link survives round-tripping through the whole cycle again."""
        s = seed()
        _reclaim_then_log(client, s)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert len(_squat_rows(s)) == 1

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder again").status_code == 200

        client.force_login(s.athlete)
        assert _log_session_as_rendered(client, s).status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.load, r.reps) for r in rows]
        assert (rows[0].load, rows[0].reps) == ("225", "5")
