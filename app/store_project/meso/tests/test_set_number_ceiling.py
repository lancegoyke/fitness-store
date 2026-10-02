"""Issue #570: set numbers have a ceiling, and the typed path refuses honestly.

``MAX_LOGGED_SET_NUMBER`` (50) bounds the set numbers ``_upsert_parsed_set``
(the typed path) will mint, and the athlete page lists a row past it only as
read-only history. The structured Set-row logger's own ceiling tests -- the
collision-renumbering walk's 400 refusal, and the replace-delete sparing a row
past the ceiling -- were deleted with that logger (#578 stage 4): it no longer
writes or deletes sets. What remains pins the typed path's ceiling behaviour and
the presenter's treatment of an out-of-range legacy row.
"""

import pytest

from store_project.meso import presenters
from store_project.meso.models import MAX_LOGGED_SET_NUMBER
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


class TestUpsertDeclinesPastTheCeiling:
    """#570: ``_upsert_parsed_set`` must decline, not mint, past the ceiling.

    Every number in the legal range is already taken by plain structured rows
    (``source_line=None``) — a shape ``_upsert_parsed_set``'s own savepoint
    never deletes, since it only ever deletes rows whose ``source_line`` IS
    the cell just blurred (see its ``mine`` lookup). That makes the range
    genuinely exhausted by the time a brand-new sub-line's blur asks for a
    free number, no matter which line the athlete types into.
    """

    def test_a_new_sub_line_declines_rather_than_minting_an_out_of_range_row(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        for number in range(1, MAX_LOGGED_SET_NUMBER + 1):
            LoggedSet.objects.create(
                session_log=log,
                prescription=s.squat,
                set_number=number,
                reps="5",
                load="100",
                rpe="",
            )

        # A brand-new sub-line (line 1 has never been written before) whose
        # text parses as a real set.
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # The cell's text is saved either way — declining to mint a row must
        # never cost the athlete their typed text.
        assert sub_cell(s.squat, 1).text == "225 x 5"
        assert not LoggedSet.objects.filter(
            set_number__gt=MAX_LOGGED_SET_NUMBER
        ).exists()
        # Nothing backs the line: the set-shaped text resolved, but every
        # legal number was taken, so no row was minted for it at all.
        assert body["cell"]["warn_reason"] == "unlogged"


class TestUpsertFreedNumberFallback:
    """#570 round 2: replacing an out-of-range row must not delete-and-lose it.

    ``_first_free_set_number`` only scans ``1..MAX_LOGGED_SET_NUMBER``. A row
    THIS sub-line already held above that ceiling — left there by the old
    unbounded renumbering walk, before this same round bounded it too — frees
    a number outside that scan the instant ``_upsert_parsed_set`` deletes it
    to replace it with the edited performance. Before the ``freed_numbers``
    fallback (see the "#570 round 2" comment above ``_upsert_parsed_set``'s
    ``mine`` lookup), an exercise whose legal range was otherwise fully
    occupied made the helper answer "nothing free", and the athlete's
    performed set was deleted with nothing put back in its place — a 200
    response, no error anywhere, and a set they actually did just vanished.
    """

    def test_replacing_an_out_of_range_row_lands_on_the_number_it_freed(self, client):
        s = seed()
        client.force_login(s.athlete)
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        # Every legal number already taken by a plain structured row (no
        # `source_line`) — what makes `_first_free_set_number` come back
        # empty-handed and forces the walk down to the `freed_numbers`
        # fallback, rather than just landing on an ordinary free slot.
        for number in range(1, MAX_LOGGED_SET_NUMBER + 1):
            LoggedSet.objects.create(
                session_log=log,
                prescription=s.squat,
                set_number=number,
                reps="5",
                load="100",
                rpe="",
            )
        cell = sub_line(s.squat, "225 x 5", line=1, athlete_authored=True)
        stranded = LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            source_line=cell,
            set_number=MAX_LOGGED_SET_NUMBER + 5,
            reps="5",
            load="225",
            rpe="",
        )

        # An ordinary edit — a genuinely different value on the same line,
        # not a no-op re-blur of the same text.
        resp = write_cell(client, s.session, s.squat, 1, "225 x 6")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        rows = LoggedSet.objects.filter(session_log=log, source_line=cell)
        assert rows.count() == 1
        row = rows.get()
        # Delete-then-recreate, like every other edit on this line — not an
        # in-place update of the stranded row.
        assert row.pk != stranded.pk
        assert row.reps == "6"
        assert row.load == "225"
        # On the number the deleted row freed — the one number
        # `_first_free_set_number`'s bounded scan can never reach on its own.
        assert row.set_number == MAX_LOGGED_SET_NUMBER + 5

    def test_a_row_belonging_to_another_exercise_is_spared_not_deleted(self, client):
        """A blur on one exercise must never delete another exercise's row.

        The round-3 review built this state and found the branch DELETING the
        row and creating nothing — a performed set destroyed on a 200. The
        chain was: ``mine`` filtered on ``source_line`` alone, so it picked up
        a row stamped with a DIFFERENT line-0 cell (``rdl``) than the one
        being edited (``squat``); the delete ran; ``taken`` is scoped to
        ``squat`` and was full, so neither the bounded scan nor the
        freed-number fallback (the freed number, 3, is one squat's own row
        holds) could place a replacement. Net: one fewer performance, nothing
        on screen to say so.

        ``mine`` is now scoped to ``line_zero_cell`` as well, so the row is
        never a candidate for this delete in the first place. Sparing a row
        this path cannot account for is the same call the replace-delete's own
        trainable/hidden skips make. Built directly via the ORM: the shape is
        incoherent data, and the code has to hold regardless of how it arose.
        """
        s = seed()
        client.force_login(s.athlete)
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        for number in range(1, MAX_LOGGED_SET_NUMBER + 1):
            LoggedSet.objects.create(
                session_log=log,
                prescription=s.squat,
                set_number=number,
                reps="5",
                load="100",
                rpe="",
            )
        cell = sub_line(s.squat, "225 x 5", line=1, athlete_authored=True)
        # Points at squat's sub-line, but belongs to RDL.
        foreign = LoggedSet.objects.create(
            session_log=log,
            prescription=s.rdl,
            source_line=cell,
            set_number=3,
            reps="5",
            load="225",
            rpe="",
        )

        resp = write_cell(client, s.session, s.squat, 1, "225 x 6")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # THE point of this test: the performance survives. Before the scoping
        # fix this row was gone, with nothing created in its place.
        foreign.refresh_from_db()
        assert foreign.reps == "5"
        assert foreign.load == "225"
        assert foreign.set_number == 3
        # Squat's own row at 3 is untouched too — nothing collided with it.
        untouched = LoggedSet.objects.get(
            session_log=log, prescription=s.squat, set_number=3
        )
        assert untouched.reps == "5"
        assert untouched.load == "100"
        # Squat's line still has no row of its own (the legal range is full),
        # and says so rather than pretending otherwise.
        assert not LoggedSet.objects.filter(
            source_line=cell, prescription=s.squat
        ).exists()
        assert body["cell"]["warn_reason"] == "unlogged"


class TestPresenterHardCap:
    def test_a_row_past_the_ceiling_is_listed_read_only_never_as_a_line(self):
        s = seed()
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        # A legacy structured row past the ceiling -- e.g. left over from before
        # #570's fix, or a stray write some other path allowed through. The page
        # lists it as read-only history and never opens an input line for its
        # number (the client caps lines at ``pad_lines`` / MAX_CELL_LINE).
        LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=MAX_LOGGED_SET_NUMBER + 5,
            reps="5",
            load="225",
            rpe="8",
        )

        rendered = presenters.athlete_session(s.session, s.athlete)
        squat = next(e for e in rendered["exercises"] if e["id"] == s.squat.pk)

        assert [r["set_number"] for r in squat["logged_readonly"]] == [
            MAX_LOGGED_SET_NUMBER + 5
        ]
        assert squat["pad_lines"] <= MAX_LOGGED_SET_NUMBER
