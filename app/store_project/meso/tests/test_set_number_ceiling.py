"""Issue #570: the collision-renumbering walk has a ceiling, and refuses honestly.

``athlete_log_session``'s renumbering loop moves a surviving PARSED row off a
set number the client just posted (see the "Move any surviving PARSED row"
comment there). It used to walk ``number += 1`` with no upper bound, while
``_clean_logged_sets`` rejects a posted ``set_number`` above
``MAX_LOGGED_SET_NUMBER`` (50) and ``presenters._set_rows`` rendered up to
``hard_cap=60``. A survivor could climb to 51+, the presenter would still draw
it as an ordinary fillable Set row, and the moment the athlete filled or
ticked it the endpoint rejected that number and 400'd the WHOLE payload — a
hard lockout, with no way to save anything else in that session either.

The fix (decided, not designed here): the walk is bounded to
``1..MAX_LOGGED_SET_NUMBER``, tried upward from the row's own number first and
then from the bottom. When genuinely nothing is free in that range, the save
is refused outright — an ``HttpResponseBadRequest`` naming the exercise — and
writes nothing, rather than leaving two rows on one number. The presenter's
``hard_cap`` now defaults to the same constant, so the grid can never render a
row the endpoint would go on to reject.
"""

import pytest

from store_project.meso import presenters
from store_project.meso.models import MAX_LOGGED_SET_NUMBER
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def _fill_every_number_with_hidden_rows(log, cell):
    """50 PARSED ``LoggedSet`` rows on ``cell``, one per number in the legal range.

    Each gets its own sub-line (``sub_line``) whose text still renders that
    exact performance — the same test ``hidden_parsed_set_pks`` uses — so
    every one of the 50 is a SURVIVOR the replace-delete spares automatically
    (``athlete_log_session`` never deletes a row a sub-line is still
    displaying). That is what makes the full range genuinely occupied by the
    time the renumbering loop below runs, not just wiped and recreated from
    the posted payload like an ordinary structured row would be.
    """
    rows = {}
    for number in range(1, MAX_LOGGED_SET_NUMBER + 1):
        cell_n = sub_line(cell, "100 x 5", line=number)
        rows[number] = LoggedSet.objects.create(
            session_log=log,
            prescription=cell,
            source_line=cell_n,
            set_number=number,
            reps="5",
            load="100",
            rpe="",
        )
    return rows


class TestCollisionWalkCeiling:
    def test_refuses_the_save_and_writes_nothing_when_every_number_is_taken(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        log = SessionLog.objects.create(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.PENDING,
            notes="pre-refusal notes",
        )
        rows = _fill_every_number_with_hidden_rows(log, s.squat)

        # A plain structured row (no `source_line`/`reclaimed_line`) — none of
        # the 50 hidden rows above is one. `athlete_log_session`'s
        # replace-delete sweeps EVERY such row unconditionally (the
        # `row.source_line_id is not None and not _client_held(...)` guard
        # only ever fires for a row that HAS a `source_line`), regardless of
        # whether this payload posts it, so this one lands in `replaceable`
        # and gets `.delete()`d before the renumbering loop below ever runs —
        # which is what makes it prove the rollback rather than the set-number
        # bookkeeping the rest of this test already covers.
        structured = LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=2,
            reps="3",
            load="150",
            rpe="",
        )

        # The client posts a NEW performance at set_number 1 — a different
        # value than the hidden row sitting there, so it can't be absorbed as
        # a mere restatement of that row (see the "twin" absorption
        # ``athlete_log_session`` does before renumbering runs). That row
        # must move aside, but every number in the legal range — including
        # the one just posted — is already taken.
        resp = log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "10",
                        "load": "200",
                        "rpe": "8",
                    }
                ]
            },
        )

        assert resp.status_code == 400
        # JSON, not plain text: the client renders `error` to the athlete
        # verbatim, and only a refusal written for them carries that shape
        # (the endpoint's validation 400s stay bare text so they can't).
        assert resp.json() == {
            "ok": False,
            "error": "Too many sets logged for Box Squat.",
        }

        # Nothing was written: the atomic block's rollback undoes even the
        # earlier `.delete()` this save ran before reaching the renumbering
        # loop — a bare `return` from inside it would have committed that
        # delete and refused the save anyway, silently losing the rows.
        assert LoggedSet.objects.filter(session_log=log).count() == 51
        for number, row in rows.items():
            row.refresh_from_db()
            assert row.set_number == number
            assert row.reps == "5"
            assert row.load == "100"
        # Raises DoesNotExist if the earlier `.delete()` actually committed —
        # this is the row two independent reviewers pointed out the old
        # version of this test never checked: `replaceable` in THAT version
        # was empty (every fixture row had a `source_line`), so the delete was
        # a no-op and this assertion couldn't have told the rollback apart
        # from its absence. With this row in the mix, the delete is genuinely
        # non-empty.
        structured.refresh_from_db()
        assert structured.set_number == 2
        assert structured.reps == "3"
        assert structured.load == "150"
        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 1
        )
        # `log.save()` — which flips PENDING to DONE and resets `notes` to
        # whatever this payload posted ("", since it posts none) — runs
        # BEFORE the renumbering loop refuses the save, and it is a write
        # `transaction.set_rollback(True)` must undo just as much as the
        # `.delete()` above: without it, this commits regardless of the 400.
        # (Verified by hand: commenting out `transaction.set_rollback(True)`
        # in `athlete_log_session` turns both of these green assertions red —
        # `status` reads back `"done"` and `notes` reads back `""`.)
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING
        assert log.notes == "pre-refusal notes"


class TestReplaceDeleteSparesOutOfRangeRow:
    """#570: a row the presenter can't render is history, not draft state.

    ``athlete_log_session``'s replace-delete now skips any row with
    ``set_number > MAX_LOGGED_SET_NUMBER`` before deciding whether to sweep
    it — see the "#570: a row the logger cannot RENDER" comment there. Without
    that skip this row is a plain structured row (``source_line=None``), so
    the same unconditional sweep ``TestCollisionWalkCeiling`` above relies on
    would delete it outright, and nothing in the posted payload could ever
    bring it back (the client can't name a set_number the presenter never
    rendered).
    """

    def test_a_row_past_the_ceiling_survives_an_ordinary_save(self, client):
        s = seed()
        client.force_login(s.athlete)
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="200",
            rpe="8",
        )
        out_of_range = LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=MAX_LOGGED_SET_NUMBER + 5,
            reps="3",
            load="300",
            rpe="9",
        )

        # An ordinary save reposting only the in-range row — the presenter
        # never renders the out-of-range one, so no real client payload could
        # ever name it.
        resp = log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "200",
                        "rpe": "8",
                    }
                ]
            },
        )

        assert resp.status_code == 200
        out_of_range.refresh_from_db()
        assert out_of_range.set_number == MAX_LOGGED_SET_NUMBER + 5
        assert out_of_range.reps == "3"
        assert out_of_range.load == "300"
        # The in-range row is fair game for the ordinary replace/recreate
        # cycle; only its value is pinned, not its identity (a fresh row
        # absorbing the same repost is just as correct).
        assert LoggedSet.objects.filter(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="200"
        ).exists()


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
    def test_set_rows_never_renders_past_the_ceiling(self):
        s = seed()
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        # A structured row past the ceiling — e.g. left over from before #570's
        # fix, or a stray write some other path allowed through. Whatever put
        # it there, the presenter must never draw it as a fillable Set row: a
        # save that reposted it would itself be rejected by
        # ``_clean_logged_sets``' own ``MAX_LOGGED_SET_NUMBER`` check.
        LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=MAX_LOGGED_SET_NUMBER + 5,
            reps="5",
            load="225",
            rpe="8",
        )

        rendered = presenters.athlete_session(s.session, s.athlete)
        rendered_numbers = [
            row["set_number"]
            for exercise in rendered["exercises"]
            for row in exercise["set_rows"]
        ]

        assert rendered_numbers
        assert max(rendered_numbers) <= MAX_LOGGED_SET_NUMBER
