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
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        rows = _fill_every_number_with_hidden_rows(log, s.squat)

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
        assert resp.content == b"Too many sets logged for Box Squat."

        # Nothing was written: the atomic block's rollback undoes even the
        # earlier `.delete()` this save ran before reaching the renumbering
        # loop — a bare `return` from inside it would have committed that
        # delete and refused the save anyway, silently losing the rows.
        assert LoggedSet.objects.filter(session_log=log).count() == 50
        for number, row in rows.items():
            row.refresh_from_db()
            assert row.set_number == number
            assert row.reps == "5"
            assert row.load == "100"
        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 1
        )


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
            set_number=55,
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
