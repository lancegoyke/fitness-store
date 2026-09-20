"""Row identity in the logged-sets payload (#567), and its #568 twin.

#567: ``athlete_log_session`` used to decide what a posted row *means* by
matching ``(prescription, set_number, values)`` — but a hidden row isn't on
screen, so a stale tab's ``set_number`` is evidence about a render that may be
several saves old, and the row's own number can move under it (the
renumbering loop exists precisely to move a surviving row off a set number a
save just claimed). Two failures followed: a stale repost that no longer
lines up with the row it meant created a silent DUPLICATE (A below), and a
genuinely new performance typed into an empty-looking Set row that happened
to sit over a hidden twin was read as a restatement and SWALLOWED (B below).

The fix is row identity in the payload: each posted set may carry the
server's own ``id`` (a row the client rendered) or a client-minted
``client_id`` (a row with no server id yet), at most one of the two.
``identified`` is a whole-payload property — every set in a request either
carries one of the two, or (a legacy/stale-tab client with neither) the
WHOLE request falls back to today's positional match, byte-for-byte. A mixed
payload can't happen from a real client (it always tags every row it knows
how to), so "all or nothing" is the simplest rule that can't be gamed by a
partially-upgraded page.

#568 is unrelated in cause but lives here too (a separate class below): the
same "which log is this cell backed by" question, asked by the blur response
and the presenter, must get the same answer.
"""

import datetime
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.serializers import serialize_session_log
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.meso.tests.test_reclaim_restore_after_log import _reclaim_then_log
from store_project.meso.tests.test_reclaim_restore_after_log import _squat_rows

pytestmark = pytest.mark.django_db


def _row_tuples(rows):
    return [
        (r.source_line_id, r.reclaimed_line_id, r.set_number, r.load, r.reps)
        for r in rows
    ]


# -- #567 A: a stale tab's repost no longer duplicates a renumbered hidden row --


class TestStaleRepostNoLongerDuplicates:
    def test_567_a_exactly_as_the_issue_describes_it(self, client):
        """The parsed-row variant, no ``reclaimed_line`` involved.

        1. athlete writes ``225 x 5`` on sub-line 1 -> parsed row A;
        2. coach rewrites the line, so A shows as a Set row;
        3. capture the payload tab T would post (WITH A's own id, exactly as
           ``serialize_session_log`` already echoes it and a current client
           posts it back);
        4. the line's text goes back to ``225 x 5`` -- A is hidden again (a
           direct retype reaches the same end state #561's coach-undo route
           does, without pulling in the reclaim/#541 carry machinery this
           scenario doesn't need);
        5. from a SECOND device the athlete logs a different set into Set row
           1, which renumbers the hidden row off it;
        6. tab T posts its unchanged, stale payload.

        On ``main`` this ends with two rows for one performance (quoted in
        the issue as ``[(3, None, None, 1, '225', '5'), (1, 3, None, 2, '225',
        '5')]`` in ``(pk, source_line_id, reclaimed_line_id, set_number,
        load, reps)`` form). After the fix it must be ONE row -- the
        surviving hidden row A, absorbed by its own id.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        a_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        assert [row["id"] for row in rendered] == [a_pk], (
            "A must be the one visible Set row at this point"
        )
        stale_payload = {
            "status": "done",
            "sets": [
                {
                    "id": row["id"],
                    "prescription": row["prescription"],
                    "set_number": row["set_number"],
                    "reps": row["reps"],
                    "load": row["load"],
                    "rpe": row["rpe"],
                }
                for row in rendered
            ],
        }

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert [r.pk for r in _squat_rows(s)] == [a_pk], (
            "the retype must reuse row A in place, not duplicate it"
        )

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "135",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        a_row = LoggedSet.objects.get(pk=a_pk)
        assert a_row.set_number == 2, "the hidden row must be renumbered off set 1"

        resp = log_post(client, s.session, stale_payload)
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert _row_tuples(rows) == [(cell.pk, None, 2, "225", "5")], (
            "the stale repost must be absorbed by the id it names, one row "
            f"survives: {[(r.pk, *t) for r, t in zip(rows, _row_tuples(rows))]}"
        )
        # The different set logged from the second device (135 x 8) is STILL
        # gone here -- that is `athlete_log_session`'s ordinary wholesale
        # replace (a save replaces every structured row its payload didn't
        # repost), not a row-identity bug. #567 was only ever about the
        # DUPLICATE this save used to create; last-write-wins on an
        # unrelated set typed elsewhere is unrelated, pre-existing, and out
        # of scope here.
        assert resp.json()["log"]["sets"] == []


# -- #567 B: a genuine second performance is no longer swallowed ------------


class TestClientIdPreventsASwallowedSet:
    def test_567_b_exactly_as_the_issue_describes_it(self, client):
        """A hidden parsed row, then a second identical performance.

        Typed into the empty-looking Set row it occupies -- posted with a
        ``client_id`` and no ``id``, exactly as a current client would for a
        grid row that has no server row yet.

        On ``main`` this ends ``rows: [(1, 3, None, 1, '225', '5')]`` with
        ``log.sets == []`` -- the second set silently lost. After the fix:
        two rows survive, and the response echoes exactly the new one, with
        its ``client_id`` round-tripped.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "client_id": "row-1",
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

        rows = _squat_rows(s)  # ordered by set_number
        assert _row_tuples(rows) == [
            (None, None, 1, "225", "5"),
            (cell.pk, None, 2, "225", "5"),
        ], (
            "the new performance must survive as its own row, not be absorbed "
            f"by the hidden one: {[(r.pk, *t) for r, t in zip(rows, _row_tuples(rows))]}"
        )

        log_sets = resp.json()["log"]["sets"]
        assert len(log_sets) == 1, (
            "only the new row is visible; the hidden one stays hidden"
        )
        new_set = log_sets[0]
        assert new_set["client_id"] == "row-1"
        assert (new_set["set_number"], new_set["load"], new_set["reps"]) == (
            1,
            "225",
            "5",
        )


# -- The legacy (id-less) fallback is exercised and pinned, not just spared --


class TestLegacyFallbackStillTakesThePositionalPath:
    def test_an_idless_payload_still_swallows_the_new_set_known_limit(self, client):
        """No ``id``/``client_id`` at all -> unidentified, positional path.

        This is the #567 B scenario again, but from a stale (pre-deploy)
        client that cannot send either field. The absorb still reads the
        posted row as a restatement of the hidden one and drops it -- a
        DELIBERATE, pinned limit of the fallback (a client that can't name
        its rows can't be told apart from one that's merely re-describing a
        row it can already see), not a regression: a current client always
        sends ``client_id`` for a grid row with no server id, which is
        exactly the case the fix above stops swallowing.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        a_pk = LoggedSet.objects.get(prescription=s.squat).pk

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

        rows = _squat_rows(s)
        assert [r.pk for r in rows] == [a_pk], (
            "known limit: the fallback absorbs the new set"
        )
        assert resp.json()["log"]["sets"] == []

    def test_an_idless_payload_still_replaces_a_visible_parsed_row(self, client):
        """No ids anywhere still takes the #541/#561 positional path.

        A stale client replaces a visible parsed row rather than duplicating
        it. Deeper coverage of this path lives in
        ``test_reclaim_restore_after_log.py``; this pins it here too, next
        to the identified-path tests it must keep behaving like.
        """
        s = seed()
        _reclaim_then_log(client, s)  # `_log_session_as_rendered` posts no ids at all
        assert len(_squat_rows(s)) == 1


# -- Unit coverage of `_clean_logged_sets`'s new validation ------------------


def _post_sets(client, s, sets):
    return log_post(client, s.session, {"status": "pending", "sets": sets})


class TestCleanLoggedSetsRowIdentityValidation:
    def _one(self, **overrides):
        base = {
            "prescription": None,
            "set_number": 1,
            "reps": "5",
            "load": "225",
            "rpe": "",
        }
        base.update(overrides)
        return base

    def test_id_true_is_rejected_bool_is_an_int_subclass(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, id=True)])
        assert resp.status_code == 400

    def test_id_zero_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, id=0)])
        assert resp.status_code == 400

    def test_id_negative_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, id=-1)])
        assert resp.status_code == 400

    def test_id_non_int_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, id="1")])
        assert resp.status_code == 400

    def test_duplicate_id_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        a_pk = LoggedSet.objects.get(prescription=s.squat).pk
        resp = _post_sets(
            client,
            s,
            [
                self._one(prescription=s.squat.pk, set_number=1, id=a_pk),
                self._one(prescription=s.rdl.pk, set_number=1, id=a_pk),
            ],
        )
        assert resp.status_code == 400

    def test_client_id_non_string_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, client_id=1)])
        assert resp.status_code == 400

    def test_client_id_blank_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(client, s, [self._one(prescription=s.squat.pk, client_id="")])
        assert resp.status_code == 400

    def test_client_id_whitespace_only_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(
            client, s, [self._one(prescription=s.squat.pk, client_id="   ")]
        )
        assert resp.status_code == 400

    def test_client_id_too_long_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(
            client, s, [self._one(prescription=s.squat.pk, client_id="x" * 65)]
        )
        assert resp.status_code == 400

    def test_client_id_at_max_length_is_accepted(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(
            client, s, [self._one(prescription=s.squat.pk, client_id="x" * 64)]
        )
        assert resp.status_code == 200

    def test_duplicate_client_id_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(
            client,
            s,
            [
                self._one(prescription=s.squat.pk, set_number=1, client_id="dup"),
                self._one(prescription=s.rdl.pk, set_number=1, client_id="dup"),
            ],
        )
        assert resp.status_code == 400

    def test_both_id_and_client_id_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = _post_sets(
            client, s, [self._one(prescription=s.squat.pk, id=1, client_id="row-1")]
        )
        assert resp.status_code == 400


# -- #568: the blur response and the next render must agree ------------------


class TestSubLineWarnAgreesAcrossSurfaces:
    """The blur response and the next render must agree on a line's tint.

    ``sub_line_should_warn``'s fallback used to match a ``LoggedSet`` on ANY
    ``SessionLog`` in the database, while the presenter always reads one
    specific log. The two ways they can diverge, per the issue: a stray
    second log for the same (session, athlete), and a coach move that takes
    the cell to another day while the ``LoggedSet`` stays behind.
    """

    def test_a_second_log_for_the_same_session_athlete_does_not_back_the_line(
        self, client
    ):
        s = seed()
        # A coach-authored sub-line the athlete never touched, so a blur that
        # re-posts its own unchanged text is a no-op (`untouched_coach_line`)
        # and never re-derives a fresh backing row -- the only way to observe
        # `_cell_warn_or_false`'s read without it healing the very gap this
        # test means to catch.
        cell = sub_line(s.squat, "225 x 5", line=1)
        old_log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        LoggedSet.objects.create(
            session_log=old_log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            rpe="",
        )
        SessionLog.objects.filter(pk=old_log.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=1)
        )
        # The newest log for this (session, athlete) -- the one the presenter
        # and (once fixed) the blur response both read -- has NO sets at all.
        SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        ctx = presenters.athlete_session(s.session, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is True, (
            "the newest log has no set backing this line -- both surfaces "
            f"must call it unlogged/tinted (blur={blur_warn}, render={render_warn})"
        )

    def test_a_moved_exercise_reads_unlogged_on_its_new_day(self, client):
        """After a move, the cell travels but the ``LoggedSet`` doesn't.

        ``prescription_move`` moves the ``ExerciseSlot`` to the new day; the
        ``LoggedSet`` stays on the old day's log. The DECIDED answer (#568)
        is that the line now reads unlogged on the new day -- a behavior
        change for ordinary parsed rows, taken deliberately rather than left
        to the two surfaces to disagree about.
        """
        s = seed()
        day2 = day(s.week, day_number=2, name="Upper", bias="Push")
        cell = sub_line(s.squat, "225 x 5", line=1)
        old_log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        LoggedSet.objects.create(
            session_log=old_log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            rpe="",
        )

        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_move",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"session_id": day2.pk, "index": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        resp = write_cell(client, day2, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        ctx = presenters.athlete_session(day2, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is True, (
            "the old day's set must not back the line on its new day -- both "
            f"surfaces must read unlogged (blur={blur_warn}, render={render_warn})"
        )
