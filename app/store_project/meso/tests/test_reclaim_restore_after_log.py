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

import json

import pytest
from django.urls import reverse

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
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


class TestUndoSparesACellAStructuredCopyPointsAt:
    """Undo must not delete a line a structured copy still answers to.

    Modelled on ``TestUndoDoesNotOrphanASetsSourceCell``
    (test_parse_at_commit.py), but the surviving row here is the source-less
    structured copy a "Log session" left behind. The stray-cell cleanup spared
    only lines a parsed row points at, so an undo past the line's creation
    deleted it, the link went NULL, and the restore minted a twin again.
    """

    def test_a_cell_backing_a_structured_copy_survives_a_restore(self, client):
        s = seed()
        client.force_login(s.coach)
        # A snapshot that predates the athlete's line.
        assert reclaim(client, s, text="tempo cue", line=3).status_code == 200

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder", line=1).status_code == 200

        client.force_login(s.athlete)
        assert _log_session_as_rendered(client, s).status_code == 200
        copy = LoggedSet.objects.get(prescription=s.squat, source_line__isnull=True)
        assert copy.reclaimed_line_id == cell.pk

        client.force_login(s.coach)
        undo_url = reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk})
        for _ in range(3):
            client.post(undo_url, content_type="application/json")

        assert Prescription.objects.filter(pk=cell.pk).exists(), (
            "undo hard-deleted a cell a structured copy still points at"
        )
        copy.refresh_from_db()
        assert copy.reclaimed_line_id == cell.pk, (
            "undo cleared the link a structured copy still answers to"
        )

        redo_url = reverse("meso:api_plan_redo", kwargs={"plan_id": s.plan.pk})
        for _ in range(3):
            client.post(redo_url, content_type="application/json")

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, [
            (r.pk, r.source_line_id, r.reclaimed_line_id, r.load, r.reps) for r in rows
        ]
        assert (rows[0].load, rows[0].reps) == ("225", "5")


class TestTheCopyIsOnlyReusedWhenTheLineShowedNoSetOfItsOwn:
    """The ``reclaimed_line`` fallback is a restore, so ``mine`` must be empty.

    When the line is showing a set of its own, the blur edits THAT set. Landing
    on the structured copy's values doesn't make it the same performance, and
    re-linking would fold two sets into one that a later clear then deletes.
    """

    def test_with_log_session_a_correction_does_not_touch_the_structured_copy(
        self, client
    ):
        s = seed()
        _reclaim_then_log(client, s)  # A -> reclaim -> Log session -> S

        resp = write_cell(client, s.session, s.squat, 1, "230 x 3")  # R_B
        assert resp.status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")  # correction
        assert resp.status_code == 200

        pairs = sorted((r.load, r.reps) for r in _squat_rows(s))
        assert pairs == [("225", "5"), ("225", "5")], (
            "the correction must mint its own row, not re-link the structured "
            f"copy meant for a genuine restore: {pairs}"
        )
        assert (
            LoggedSet.objects.filter(
                prescription=s.squat, source_line__isnull=True
            ).count()
            == 1
        ), "the structured copy must survive the correction untouched"

        resp = write_cell(client, s.session, s.squat, 1, "")  # clear
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, [(r.source_line_id, r.load, r.reps) for r in rows]
        assert rows[0].source_line_id is None, "the structured copy must survive"
        assert (rows[0].load, rows[0].reps) == ("225", "5")

    def test_without_log_session_the_line_lookup_is_unchanged(self, client):
        """The older ``source_line`` lookup keeps its main-branch behavior.

        With no "Log session" in between, the reclaimed row A still sits on
        this line, so correcting the line's own set to A's values reuses A, as
        before #541. Gating that lookup the way the fallback is gated would
        leave A and a new twin on one line, both hidden by its text, and one
        clear would then delete both.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")  # A
        a_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "230 x 3")
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        rows = _squat_rows(s)
        assert [(r.pk, r.load, r.reps) for r in rows] == [(a_pk, "225", "5")]


class TestExactlyTwoSetLoggedEventsAcrossTheCorrectionSequence:
    """The correction is an edit, not a new set — only two blurs ever mint one."""

    def test_the_correction_fires_no_third_event(self, client):
        s = seed()
        _reclaim_then_log(client, s)

        write_cell(client, s.session, s.squat, 1, "230 x 3")
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        rows = list(Event.objects.filter(name=EventName.SET_LOGGED).order_by("id"))
        assert len(rows) == 2, [r.props for r in rows]


class TestTheSkipPathIsPinned:
    """A visible parsed row can also come from a coach skip, not a reclaim.

    Not a new bug — pinning that the existing skip/unskip/Log-session/restore
    path already ends with exactly one row.
    """

    def test_skip_then_log_session_then_restore_leaves_one_row(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")  # A

        client.force_login(s.coach)
        skip_url = reverse(
            "meso:api_prescription_skip",
            kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
        )
        resp = client.post(
            skip_url,
            data=json.dumps({"skipped": True}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "felt heavy")
        assert resp.status_code == 200

        client.force_login(s.coach)
        resp = client.post(
            skip_url,
            data=json.dumps({"skipped": False}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        assert _log_session_as_rendered(client, s).status_code == 200

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, [
            (r.pk, r.source_line_id, r.reclaimed_line_id, r.load, r.reps) for r in rows
        ]
        assert (rows[0].load, rows[0].reps) == ("225", "5")
