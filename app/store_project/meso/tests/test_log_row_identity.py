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
from store_project.meso import views as meso_views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
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


# -- adversarial review round: P1-A/P1-B/P2-A/P2-B ---------------------------


class TestBlankPostedIdMustNotDestroyARowWithValues:
    """#567/#568 P1-A: a wholly blank id match must not destroy a real row.

    A wholly blank posted set must not count as HOLDING a visible parsed row
    that still carries real values. The chain, as the review found it: a
    hidden parsed row X becomes VISIBLE when the coach reclaims its sub-line.
    A stale page that saves without
    naming X spares it (nothing in its payload holds it) -- but the RESPONSE
    now includes X, since it's visible. The client's ``syncFromLog`` matches
    its own empty grid row to X by slot, ticks it, and adopts ``r.id = X.pk``
    while the row's inputs stay blank; ``rowFilled`` reads ``r.done`` alone,
    so the row's NEXT save posts ``{id: X.pk, reps: "", load: "", rpe:
    ""}``. A pure id match (no value check) then reads that as "the client is
    holding X" and deletes it, replacing it with nothing -- the performance
    is gone. Reproduced directly at the server: the blank payload is built by
    hand, since the client itself never needs to be driven through the stale
    round trip to produce it.
    """

    def test_a_wholly_blank_id_match_spares_the_row_it_names(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        x_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        assert [row["id"] for row in rendered] == [x_pk], (
            "X must be the one visible Set row at this point"
        )

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "id": x_pk,
                        "prescription": s.squat.pk,
                        "set_number": rendered[0]["set_number"],
                        "reps": "",
                        "load": "",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200

        x_row = LoggedSet.objects.get(pk=x_pk)
        assert (x_row.load, x_row.reps) == ("225", "5"), (
            "a wholly blank posted set must not destroy a row that still "
            "carries real values"
        )


class TestStaleIdReplayDegradesToPositionalNotNoMatch:
    """#567/#568 P1-B: a STALE id must degrade to the positional match.

    A STALE id (tagged, but names no row this log holds) must fall back to
    today's positional match, not be treated as "no match" at all. The
    write-ahead outbox (#527) replays a body whose first delivery already
    committed but whose response was lost -- and that body names a row the
    first delivery already deleted and recreated under a new pk. Both
    scenarios below share the same setup: the athlete logs ``225 x 5`` (row
    A), the coach reclaims the line (A becomes a visible Set row), and the
    athlete's FIRST "Log session" -- naming A by id, exactly as
    ``serialize_session_log`` handed it out -- replaces A with a copy that
    carries A's own ``reclaimed_line`` forward. They differ only in WHEN the
    write-ahead replay of that same first save lands relative to the athlete
    retyping the sub-line back.
    """

    def _replace_with_carried_copy(self, client, s):
        """The shared setup. Returns the replayable ``body`` dict."""
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        a_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        assert [row["id"] for row in rendered] == [a_pk]
        body = {
            "status": "done",
            "sets": [
                {
                    "id": a_pk,
                    "prescription": row["prescription"],
                    "set_number": row["set_number"],
                    "reps": row["reps"],
                    "load": row["load"],
                    "rpe": row["rpe"],
                }
                for row in rendered
            ],
        }
        resp = log_post(client, s.session, body)
        assert resp.status_code == 200
        rows = _squat_rows(s)
        assert len(rows) == 1
        assert rows[0].pk != a_pk, "the first save must replace A under a new pk"
        assert rows[0].reclaimed_line_id == sub_cell(s.squat, 1).pk, (
            "the copy must carry A's own reclaim link forward"
        )
        return body

    def test_replay_after_a_retype_is_absorbed_positionally_not_duplicated(
        self, client
    ):
        """The replay lands AFTER the retype -- absorbed, not duplicated.

        (Review harm 1: "the twin absorb misses, so one performance is
        stored TWICE".)
        """
        s = seed()
        body = self._replace_with_carried_copy(client, s)

        # The athlete retypes the original text -- `_upsert_parsed_set`
        # re-links the copy (source_line=cell, reclaimed_line=None) and it
        # goes back to being hidden by its own sub-line's text.
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert len(_squat_rows(s)) == 1

        # The write-ahead outbox replays the FIRST save's own body -- its
        # `id` now names a row this log no longer holds at all.
        resp = log_post(client, s.session, body)
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, (
            "a stale id must be absorbed positionally, not create a second "
            "row: "
            f"{[(r.pk, r.source_line_id, r.reclaimed_line_id, r.load, r.reps) for r in rows]}"
        )
        assert (rows[0].load, rows[0].reps) == ("225", "5")

    def test_replay_before_a_retype_still_carries_the_link_forward(self, client):
        """The replay lands BEFORE any retype -- a recreate, not an absorb.

        The RECREATED row must still carry the reclaim link, or a later
        retype mints a duplicate instead of re-linking it. (Review harm 2:
        "#541's carried reclaimed_line is dropped, reopening the duplicate
        #541 fixed".)
        """
        s = seed()
        body = self._replace_with_carried_copy(client, s)

        # The write-ahead outbox replays the FIRST save's own body BEFORE the
        # athlete ever retypes the sub-line -- the copy is still VISIBLE, so
        # this genuinely replaces it under a new pk, not an absorb.
        client.force_login(s.athlete)
        resp = log_post(client, s.session, body)
        assert resp.status_code == 200
        rows = _squat_rows(s)
        assert len(rows) == 1
        assert rows[0].reclaimed_line_id == sub_cell(s.squat, 1).pk, (
            "the RECREATED row must still carry the reclaim link forward, or "
            "the retype below cannot re-link it"
        )

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert len(rows) == 1, (
            "the dropped link reopened #541's duplicate: "
            f"{[(r.pk, r.source_line_id, r.reclaimed_line_id, r.load, r.reps) for r in rows]}"
        )
        assert (rows[0].load, rows[0].reps) == ("225", "5")


class TestPrescriptionGuardOnIdentifiedMatches:
    """#567/#568 P2-A: an identified match also requires the SAME prescription.

    Every identified match requires the SAME prescription as the row it
    names -- pk equality alone is not enough. Crafted-payload only (no legitimate client ever posts a real id under
    the wrong prescription), but the carried-link version can hang exercise
    A's sub-line on a row under exercise B as ``reclaimed_line``, and a later
    coach undo would then hide the athlete's exercise-B row.
    """

    def test_a_valid_id_under_the_wrong_prescription_does_not_delete_the_row(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.rdl, 1, "185 x 5")
        x_pk = LoggedSet.objects.get(prescription=s.rdl).pk

        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_cell_line_write",
                kwargs={"plan_id": s.plan.pk, "slot_id": s.rdl.exercise_slot.pk},
            ),
            data=json.dumps({"week_id": s.week.pk, "line": 1, "text": "brace harder"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        assert [row["id"] for row in rendered] == [x_pk], (
            "X must be the one visible Set row at this point"
        )

        # A crafted payload: X's real pk, but claimed under squat -- a
        # DIFFERENT prescription than X actually belongs to (rdl).
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "id": x_pk,
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

        assert LoggedSet.objects.filter(pk=x_pk).exists(), (
            "a valid id under the WRONG prescription must not delete the "
            "row it names -- pk equality alone is not enough"
        )
        x_row = LoggedSet.objects.get(pk=x_pk)
        assert (x_row.load, x_row.reps) == ("185", "5"), (
            "the row's own performance must survive untouched"
        )


class TestMixedTaggedAndUntaggedPayloadIsMalformed:
    """#567 P2-B: a payload that mixes tagged and untagged sets is a 400.

    No shipped client emits this shape -- a client on this contract tags
    every set it knows how to, always -- so it demoted the WHOLE request to
    the legacy positional path silently, hiding a client bug behind the same
    fallback a genuinely old client uses on purpose.
    """

    def test_a_mixed_tagged_and_untagged_payload_is_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "id": 1,
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    },
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "",
                    },
                ],
            },
        )
        assert resp.status_code == 400


# -- adversarial review round 2: P1-E/F, P1-G, P1-H, P3 ----------------------


class TestOrdinaryTwoSetSaveNeverLetsAClientHeldIdReachAnUnrenderedRow:
    """#567/#568 P1-E/F root cause: the client must never post an id for a row it never rendered.

    And if it did, the server (correctly, by design) would trust it and
    destroy the row that id names. Three independent reviewers traced the
    same root cause to ``syncFromLog``'s slot fallback (client-side,
    ``meso_athlete.js``): it used to run over EVERY grid row, including one
    this payload never posted, and could silently plant a live row's own pk
    onto an empty, never-rendered grid row. The next ordinary edit into that
    grid row then posts the planted id, and the server -- correctly, since an
    anchored id IS the client's strongest possible proof it is looking at a
    row -- deletes the row that id actually names. The fix for that lives
    entirely in ``meso_athlete.js``/``syncFromLog`` and is covered by the
    vitest suite (frontend/meso_athlete.test.js); this class only has the
    server half of the story:

    1. a direct PIN that a payload carrying that mistaken id -- exactly what
       the OLD ``syncFromLog`` bug would have made the client hold -- is
       trusted by the server and DOES destroy the row it names. This is not
       a server bug to fix; it is exactly why the fix has to live client-side.
    2. the actual guarantee: when the SAME sequence's last save posts only
       what a page that never learned about the hidden row would ever
       render -- no id at all for the row it never saw -- the hidden row
       survives, renumbered out of the way like any other survivor.
    """

    def _through_step_three(self, client, s):
        """Steps 1-3 from the issue, shared by both scenarios below.

        1. the athlete types "135 x 8" on sub-line 2 -> hidden parsed row P;
        2. the coach rewrites sub-line 2 to a cue -> P becomes a VISIBLE Set
           row at slot 2, but the athlete's open page never reloads to learn
           it exists;
        3. the athlete saves set 1 (a fresh ``client_id`` -- the only row
           their stale page has anything typed into). P is untouched here,
           spared by ``_client_held`` (a ``client_id`` set holds nothing).

        Returns ``(p_pk, row1_pk)``.
        """
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 2, "135 x 8")
        p_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder", line=2).status_code == 200

        client.force_login(s.athlete)
        resp = log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "client_id": "grid-row-1",
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
        log_sets = resp.json()["log"]["sets"]
        assert {row["set_number"] for row in log_sets} == {1, 2}, (
            "P must already be VISIBLE (unhidden by the coach's rewrite) and "
            f"echoed in the response alongside the new row: {log_sets}"
        )
        row1_pk = next(
            row["id"] for row in log_sets if row["client_id"] == "grid-row-1"
        )
        assert next(row["id"] for row in log_sets if row["set_number"] == 2) == p_pk
        return p_pk, row1_pk

    def test_a_client_held_mistaken_id_deletes_the_row_it_names(self, client):
        """PIN, not a fix target -- documents exactly why the fix is client-side."""
        s = seed()
        p_pk, row1_pk = self._through_step_three(client, s)

        # The id a buggy `syncFromLog` (matching an unposted grid row by slot
        # alone) would have planted on grid row 2 -- P's own id.
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "id": row1_pk,
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    },
                    {
                        "id": p_pk,
                        "prescription": s.squat.pk,
                        "set_number": 2,
                        "reps": "3",
                        "load": "315",
                        "rpe": "",
                    },
                ],
            },
        )
        assert resp.status_code == 200
        assert not LoggedSet.objects.filter(pk=p_pk).exists(), (
            "an anchored id IS trusted by the server -- this is exactly why "
            "the client must never come to hold one for a row it didn't render"
        )

    def test_the_fixed_client_posting_only_what_it_rendered_spares_the_row(
        self, client
    ):
        s = seed()
        p_pk, row1_pk = self._through_step_three(client, s)
        cell = sub_cell(s.squat, 2)

        # The FIXED client's page still shows grid row 2 as empty (it never
        # learned about P) -- so the second set typed there mints its OWN
        # fresh client_id, never P's id.
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "id": row1_pk,
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    },
                    {
                        "client_id": "grid-row-2",
                        "prescription": s.squat.pk,
                        "set_number": 2,
                        "reps": "3",
                        "load": "315",
                        "rpe": "",
                    },
                ],
            },
        )
        assert resp.status_code == 200

        rows = _squat_rows(s)
        assert _row_tuples(rows) == [
            (None, None, 1, "225", "5"),
            (None, None, 2, "315", "3"),
            (cell.pk, None, 3, "135", "8"),
        ], (
            "P must survive, renumbered off the slot the new set claimed, "
            f"not be destroyed: {[(r.pk, *t) for r, t in zip(rows, _row_tuples(rows))]}"
        )
        assert LoggedSet.objects.get(pk=p_pk).set_number == 3


class TestAnchoredIdUnderWrongPrescriptionDegradesToPositional:
    """#567/#568 P1-G: an anchored id under the WRONG prescription must degrade to position.

    It must never be treated as "no match at all" -- which is what let the
    row it should have restated go unrecognized and get duplicated instead
    of absorbed.
    """

    def _setup(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.rdl, 1, "185 x 5")
        x_pk = LoggedSet.objects.get(prescription=s.rdl).pk

        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_cell_line_write",
                kwargs={"plan_id": s.plan.pk, "slot_id": s.rdl.exercise_slot.pk},
            ),
            data=json.dumps({"week_id": s.week.pk, "line": 1, "text": "keep tension"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        return s, x_pk, cell

    def test_degrades_to_positional_and_absorbs_the_row_it_restates(self, client):
        s, x_pk, cell = self._setup(client)

        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        # X's real pk (live, under rdl) -- but claimed under
                        # squat, a DIFFERENT prescription than X actually
                        # belongs to. Restates squat's own visible parsed
                        # row exactly.
                        "id": x_pk,
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

        squat_rows = _squat_rows(s)
        assert _row_tuples(squat_rows) == [(None, cell.pk, 1, "225", "5")], (
            "a mismatched-prescription id must degrade to the positional "
            "match and absorb the row it restates, not be treated as no "
            "match at all (which duplicates it instead): "
            f"{[(r.pk, *t) for r, t in zip(squat_rows, _row_tuples(squat_rows))]}"
        )
        assert LoggedSet.objects.filter(pk=x_pk).exists(), (
            "X itself, under rdl, must be untouched"
        )
        x_row = LoggedSet.objects.get(pk=x_pk)
        assert (x_row.load, x_row.reps) == ("185", "5")

    def test_reaches_the_same_end_state_as_the_id_removed(self, client):
        s, x_pk, cell = self._setup(client)

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

        squat_rows = _squat_rows(s)
        assert _row_tuples(squat_rows) == [(None, cell.pk, 1, "225", "5")], (
            "the mismatched-id payload above must reach this SAME end state "
            f"— it must degrade to exactly this positional path: "
            f"{[(r.pk, *t) for r, t in zip(squat_rows, _row_tuples(squat_rows))]}"
        )


class TestCellWarnAgreesWithAFreshSkipRead:
    """#567/#568 P1-H: ``loggable`` must come from a FRESH read, not a stale snapshot.

    ``athlete_cell_write`` builds ``line_zero`` (the exercise's line-0 cell)
    BEFORE the write transaction. ``_upsert_parsed_set`` re-reads it under
    ``select_for_update`` and acts on THAT fresh value. If
    ``_cell_warn_or_false`` instead reads the caller's stale pre-transaction
    instance, the two disagree the moment a coach's ``prescription_unskip``
    lands inside this same request's window: the request logs a REAL set
    (fresh: unskipped) but the response reports ``warn=True`` from the stale
    (skipped) snapshot -- contradicting the very set it just wrote, and a
    live counterexample to the "one answer for the tint" invariant #568
    exists to guarantee.
    """

    def test_warn_agrees_when_skip_is_lifted_mid_request(self, client, monkeypatch):
        s = seed()
        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])
        client.force_login(s.athlete)

        real_upsert = meso_views._upsert_parsed_set

        def unskip_then_upsert(session, athlete, line_zero_cell, cell, **kwargs):
            # Simulates a coach's `prescription_unskip` landing INSIDE this
            # request's window: after `athlete_cell_write` already snapshotted
            # `line_zero` (stale: skipped=True) but before the fresh, locked
            # re-read `_upsert_parsed_set` itself takes.
            Prescription.objects.filter(pk=s.squat.pk).update(skipped=False)
            return real_upsert(session, athlete, line_zero_cell, cell, **kwargs)

        monkeypatch.setattr(meso_views, "_upsert_parsed_set", unskip_then_upsert)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists(), (
            "the fresh (unskipped) read must have let this request log a real set"
        )

        ctx = presenters.athlete_session(s.session, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is False, (
            "a set really was logged this request (fresh unskip) -- the "
            "response must agree with the next render, not the caller's "
            f"stale skipped snapshot (blur={blur_warn}, render={render_warn})"
        )


class TestMixedAnchoredAndStaleIdsInOneRequest:
    """#567/#568 P3: one request can carry BOTH an anchored id and a stale one.

    For two different rows -- and each must be judged by its own rule. Every
    real write-ahead replay in which anything survived (a hidden row, a
    spared parsed row) is exactly this shape: one id naming a spared/live row
    (anchored) alongside one naming a row a first delivery already replaced
    (stale). ``TestStaleIdReplayDegradesToPositionalNotNoMatch`` only ever
    replayed single-set, all-stale bodies -- this exercises both match sites'
    rules inside ONE request and asserts each row reaches the exact end state
    it would reach alone.
    """

    def test_a_stale_squat_id_and_an_anchored_rdl_id_are_each_judged_correctly(
        self, client
    ):
        s = seed()

        # -- squat: STALE (the write-ahead-replay-after-a-retype shape from
        # TestStaleIdReplayDegradesToPositionalNotNoMatch) --
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        squat_cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        squat_row = next(row for row in rendered if row["prescription"] == s.squat.pk)
        stale_squat_set = {
            "id": squat_row["id"],
            "prescription": squat_row["prescription"],
            "set_number": squat_row["set_number"],
            "reps": squat_row["reps"],
            "load": squat_row["load"],
            "rpe": squat_row["rpe"],
        }
        first_save = log_post(
            client, s.session, {"status": "done", "sets": [stale_squat_set]}
        )
        assert first_save.status_code == 200
        squat_rows = _squat_rows(s)
        assert len(squat_rows) == 1
        assert squat_rows[0].pk != stale_squat_set["id"], (
            "the first save must replace the row under a NEW pk"
        )

        # The retype absorbs the carried copy back into being hidden again --
        # `stale_squat_set["id"]` now names a row this log no longer holds.
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert len(_squat_rows(s)) == 1

        # -- rdl: ANCHORED (a currently-live, visible parsed row, restated
        # exactly) --
        write_cell(client, s.session, s.rdl, 1, "185 x 5")
        rdl_cell = sub_cell(s.rdl, 1)
        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_cell_line_write",
                kwargs={"plan_id": s.plan.pk, "slot_id": s.rdl.exercise_slot.pk},
            ),
            data=json.dumps({"week_id": s.week.pk, "line": 1, "text": "keep tension"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        client.force_login(s.athlete)
        rdl_pk = LoggedSet.objects.get(prescription=s.rdl).pk

        # ONE request: squat's id is stale (its first delivery already
        # replaced that row), rdl's id is live (anchored).
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    stale_squat_set,  # write-ahead replay of the FIRST save
                    {
                        "id": rdl_pk,
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "185",
                        "rpe": "",
                    },
                ],
            },
        )
        assert resp.status_code == 200

        squat_rows = _squat_rows(s)
        assert _row_tuples(squat_rows) == [(squat_cell.pk, None, 1, "225", "5")], (
            "the stale squat id must still degrade to the positional absorb, "
            "unaffected by the anchored rdl id sharing the request: "
            f"{[(r.pk, *t) for r, t in zip(squat_rows, _row_tuples(squat_rows))]}"
        )
        rdl_rows = list(
            LoggedSet.objects.filter(
                session_log__session=s.session, prescription=s.rdl
            ).order_by("set_number")
        )
        assert _row_tuples(rdl_rows) == [(None, rdl_cell.pk, 1, "185", "5")], (
            "the anchored rdl id must still replace the row it restates, "
            "unaffected by the stale squat id sharing the request: "
            f"{[(r.pk, *t) for r, t in zip(rdl_rows, _row_tuples(rdl_rows))]}"
        )


# -- adversarial review round 3: P1-I ----------------------------------------


class TestAnAbsorbedSetCanLeaveAForeignRowAtItsPostedSlot:
    """#567/#568 P1-I: the state that arms the client's soundness gap.

    ``syncFromLog``'s slot fallback used to reason that the only visible row
    left at a POSTED slot, after a save, is the one the server created FOR
    that exact posted set -- true when the set was CREATED, false when it
    was ABSORBED. This class pins the ABSORBED half on the server, which is
    what makes that client-side reasoning unsound: it shows a spared VISIBLE
    parsed row can still occupy the exact slot a payload posted, and that
    ``serialize_session_log`` echoes that FOREIGN row back at that slot --
    the exact shape the client-side fix (``meso_athlete.js``,
    ``frontend/meso_athlete.test.js``) must refuse to adopt.

    Built the shortest honest way, through the real views:

    1. the athlete types "245 x 5" on sub-line 1 -> parsed row Y, hidden (its
       own line still shows it);
    2. the coach rewrites sub-line 1 to a cue -> Y becomes VISIBLE (no
       longer shown by its line's text), still at set_number 1;
    3. the athlete types "225 x 5" on sub-line 2 -> parsed row X, hidden
       (its own line still shows it), at set_number 2 -- a DIFFERENT slot
       than Y;
    4. a payload posts X's own id, X's own values, but under set_number 1 --
       Y's slot, not X's true slot 2. The twin absorb matches on pk and
       VALUES alone, with no ``set_number`` agreement (see
       ``athlete_log_session``'s twin-absorb comment), so it is absorbed by
       X regardless of the slot claimed.

    ``posted`` is recomputed AFTER the absorb (``views.py``, the twin-absorb
    loop) and the absorbed cleaned set is gone from ``cleaned_sets`` by then,
    so slot 1 never re-enters ``posted`` at all -- the collision renumbering
    (gated on ``if posted:``) never runs, and Y is left exactly where it
    was. Nothing is created. The response's item at slot 1 is therefore Y,
    not X: a different row than the one the payload named.
    """

    def test_a_survivor_can_sit_at_the_slot_an_absorbed_id_posted(self, client):
        s = seed()
        client.force_login(s.athlete)

        # Y: parsed, hidden, at set_number 1 -- then made VISIBLE by a coach
        # rewrite, so it survives the replace-delete untouched (`_client_held`
        # spares it: nothing in the payload below names it).
        write_cell(client, s.session, s.squat, 1, "245 x 5")
        y_pk = LoggedSet.objects.get(prescription=s.squat).pk

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder", line=1).status_code == 200

        client.force_login(s.athlete)
        rendered = serialize_session_log(the_log(s.session, s.athlete))["sets"]
        assert [row["id"] for row in rendered] == [y_pk], (
            "Y must be the one visible Set row at this point"
        )

        # X: parsed, hidden, at set_number 2 -- a DIFFERENT slot than Y's.
        write_cell(client, s.session, s.squat, 2, "225 x 5")
        x_pk = LoggedSet.objects.exclude(pk=y_pk).get(prescription=s.squat).pk

        # A payload naming X's own id and X's own values, but claiming Y's
        # slot (1), not X's true slot (2).
        resp = log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "id": x_pk,
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
        assert _row_tuples(rows) == [
            # Y, untouched, still at slot 1 -- still linked to its own
            # source line (the coach's rewrite only changes the line's
            # TEXT, never the row's `source_line`), just no longer HIDDEN
            # because that text no longer parses back to Y's own values.
            (sub_cell(s.squat, 1).pk, None, 1, "245", "5"),
            (sub_cell(s.squat, 2).pk, None, 2, "225", "5"),  # X, untouched
        ], (
            "both rows must survive exactly as they were -- nothing created, "
            f"nothing deleted: {[(r.pk, *t) for r, t in zip(rows, _row_tuples(rows))]}"
        )

        log_sets = resp.json()["log"]["sets"]
        assert len(log_sets) == 1, "X stays hidden; only Y is a visible Set row"
        assert log_sets[0]["id"] == y_pk, (
            "the response's item at slot 1 is Y, a DIFFERENT row than the id "
            f"(X, pk {x_pk}) the payload posted at that slot: {log_sets}"
        )
        assert (
            log_sets[0]["set_number"],
            log_sets[0]["load"],
            log_sets[0]["reps"],
        ) == (
            1,
            "245",
            "5",
        ), (
            "a client trusting the slot alone would plant Y's pk on the grid row that posted X's id"
        )
