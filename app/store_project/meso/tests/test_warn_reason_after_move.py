"""#572 part 2 — the reason a warned line carries, not just whether it warns.

A bare ``warn`` boolean can't tell an un-skip repost (the repair
`_lineNeedsSending` exists for) from a cross-day-move repost (a duplicate)
apart, so `sub_line_warn_reason`/`cell_warn_reason` now hand back a REASON
and the client acts on it (`_lineNeedsSending` in
``static/js/meso_athlete.js``: reposts a warned line unless its reason is
``"elsewhere"``).

Part 1 of #572 (a cross-day move re-tinting every already-logged week of the
block, not just the week the coach was looking at) is explicitly NOT fixed —
see ``docs/meso/decisions.md``. These tests only cover part 2: the reason a
tint carries, and what the client does with it.

Reuses the ``seed``/``write_cell``/``sub_cell`` fixtures from
``test_parse_at_commit.py`` and the ``day``/``sub_line`` builders from
``_helpers.py`` rather than duplicating them.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.models import sub_line_warn_reason
from store_project.meso.parsing import cell_warn_reason
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def move_cell(client, plan, cell, *, session_id, index=0):
    return client.post(
        reverse(
            "meso:api_prescription_move", kwargs={"plan_id": plan.pk, "pk": cell.pk}
        ),
        data=json.dumps({"session_id": session_id, "index": index}),
        content_type="application/json",
    )


def skip_cell(client, plan, cell, *, skipped):
    return client.post(
        reverse(
            "meso:api_prescription_skip", kwargs={"plan_id": plan.pk, "pk": cell.pk}
        ),
        data=json.dumps({"skipped": skipped}),
        content_type="application/json",
    )


class TestElsewhereSuppressesTheMoveRepostHazard:
    """(a) A cross-day move must not let a tinted line duplicate a set.

    Focusing and leaving a tinted line must not mint a second row — but the
    SUPPRESSION happens client-side (`_lineNeedsSending`, pinned in
    `frontend/meso_athlete.test.js`, and driven through a real browser in
    `e2e/test_meso_warn_reason_after_move.py`). This test proves the SERVER
    side of the contract: the reason the client reads is "elsewhere", a read
    alone never mutates anything, and — so the test stays honest about where
    the fix actually lives — that a client which ignored the reason and
    posted anyway would still duplicate the set.
    """

    def test_a_moved_lines_reason_is_elsewhere_and_an_actual_repost_would_duplicate(
        self, client
    ):
        s = seed()
        day2 = day(s.week, day_number=2, name="Upper", bias="Push")

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert LoggedSet.objects.count() == 1

        client.force_login(s.coach)
        resp = move_cell(client, s.plan, s.squat, session_id=day2.pk)
        assert resp.status_code == 200

        # Belt-and-braces: a page RENDER alone (the presenter, read-only) must
        # never itself create or duplicate a `LoggedSet`. The actual
        # suppression of the client's repost is `_lineNeedsSending`'s job —
        # covered by the vitest tests above and the e2e test — not this one.
        cell = sub_cell(s.squat, 1)
        ctx = presenters.athlete_session(day2, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        sub_line_1 = next(line for line in squat_ctx["sub_lines"] if line["line"] == 1)
        assert sub_line_1["warn"] is True
        assert sub_line_1["warn_reason"] == "elsewhere"
        assert LoggedSet.objects.count() == 1, (
            "rendering the athlete's session page must never mutate LoggedSet"
        )

        # "The cell-write response for the same unchanged text" — computed
        # directly via the exact helper `athlete_cell_write` uses to build
        # that field (`views._cell_warn_reason_or_blank`), rather than through
        # an ACTUAL POST: a real POST to an already athlete-authored line
        # always re-runs `_upsert_parsed_set` (see its own "authorship follows
        # actual authorship" comment — an unchanged blur is only a no-op for a
        # coach's own untouched cue), which would itself immediately create a
        # backing row on this new day and clear the reason to "" as a result.
        # That would make a read of the LIVE endpoint's response silently
        # stop being the "elsewhere" the client's blur-time check actually
        # sees and acts on. Calling the same helper the view calls, on the
        # same unmutated state the client's blur would see, is what actually
        # answers "what would this blur's response say" honestly.
        fresh_line_zero = Prescription.objects.get(pk=s.squat.pk)
        would_be_reason = views._cell_warn_reason_or_blank(
            cell, fresh_line_zero, session=day2, athlete=s.athlete
        )
        assert would_be_reason == "elsewhere"
        assert LoggedSet.objects.count() == 1, (
            "computing the would-be response must not itself mutate LoggedSet"
        )

        # Now prove the hazard is real, and that the reason is what gates it:
        # a client that ignored the reason and posted anyway (what
        # `_lineNeedsSending` used to do before #572, and what an unpatched
        # client still would) mints a SECOND `LoggedSet` for one performance —
        # the old day's row stays, hidden but still counting toward results,
        # 1RM and the agent's grounding. The fix is that the CLIENT no longer
        # sends this request at all; it is not that the server refuses one it
        # actually receives (it can't — the write is a plain idempotent
        # upsert scoped to THIS day's log, and this day has never seen it).
        client.force_login(s.athlete)
        resp = write_cell(client, day2, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert LoggedSet.objects.count() == 2, (
            "a repost of an unchanged 'elsewhere' line must still duplicate "
            "the set at the server layer -- the fix lives in the client not "
            "posting, not in the server refusing to act on one it gets"
        )


class TestUnskipStillPermitsTheRepost:
    """(b) The un-skip case must keep reposting as it does today.

    #572 must not collapse "the row wasn't accepting sets, now it is" into
    the same "elsewhere" reason a cross-day move gets, or the un-skip repair
    (`TestSkippingARowPreservesEarnedHistory` in test_parse_at_commit.py)
    stops working: the client's `_lineNeedsSending` only withholds a repost
    for `warn_reason === "elsewhere"`.
    """

    def test_unskip_reason_is_not_elsewhere_and_the_repost_still_logs(self, client):
        s = seed()

        client.force_login(s.coach)
        resp = skip_cell(client, s.plan, s.squat, skipped=True)
        assert resp.status_code == 200

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn_reason"] == "skipped"
        assert not LoggedSet.objects.exists()

        client.force_login(s.coach)
        resp = skip_cell(client, s.plan, s.squat, skipped=False)
        assert resp.status_code == 200

        # Read-only: the line's reason once the row is loggable again but
        # still has nothing backing it. Pinning the SPECIFIC string (not just
        # "truthy" or "not elsewhere") so a future change that collapses this
        # into "elsewhere" — the one reason the client won't repost — is
        # caught here rather than by a duplicated set in production.
        ctx = presenters.athlete_session(s.session, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        sub_line_1 = next(line for line in squat_ctx["sub_lines"] if line["line"] == 1)
        assert sub_line_1["warn"] is True
        assert sub_line_1["warn_reason"] == "unlogged"

        # The actual repost: the client re-sends the unchanged text now that
        # the row is loggable again, and it must still mint the set. Once the
        # set exists and backs the line, the response's own reason clears to
        # "" (no warning at all) — also pinned, so this can't quietly regress
        # into disagreeing with the presenter's next render.
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn_reason"] == ""
        assert LoggedSet.objects.filter(source_line=sub_cell(s.squat, 1)).exists()


class TestCellWarnReasonUnit:
    """(c) `parsing.cell_warn_reason`'s four outcomes.

    The parse corpus itself (what makes text "unresolved-set" vs. a real
    "set") is pinned in test_parsing.py; this only pins that the WRAPPER
    reports the specific reason string each classification maps to, not just
    a bool.
    """

    def test_a_real_set_does_not_warn(self):
        assert cell_warn_reason("225 x 5") is None

    def test_a_fat_fingered_set_attempt_is_unresolved(self):
        assert cell_warn_reason("225 x") == "unresolved"

    def test_a_set_on_a_row_that_cannot_accept_sets_is_skipped(self):
        assert cell_warn_reason("225 x 5", loggable=False) == "skipped"

    def test_a_set_too_long_to_store_is_too_long(self):
        text = "1." + "0" * 35 + " x 5"
        assert cell_warn_reason(text) == "too-long"

    @pytest.mark.parametrize(
        "text", ["skip", "-", "DB pullover", "felt tight", "20-60m", ""]
    )
    def test_skip_swap_note_duration_and_blank_never_warn(self, text):
        assert cell_warn_reason(text) is None, text


class TestSubLineWarnReasonElsewhereVsUnlogged:
    """The "elsewhere" vs. "unlogged" split #572 adds.

    A row on ANOTHER session's log that the cell's text still shows is
    "elsewhere" (the move case); no such row anywhere is "unlogged"; and a
    caller that never passes `elsewhere_sets` at all still gets "unlogged" —
    the safe default, since the un-skip repost this reason exists to keep
    firing must not silently start requiring a kwarg every caller doesn't yet
    pass.
    """

    def _cell_with_a_row_elsewhere(self, s):
        cell = sub_line(s.squat, "225 x 5", line=1)
        other_log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        row = LoggedSet.objects.create(
            session_log=other_log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            rpe="",
        )
        return cell, row

    def test_a_row_among_elsewhere_sets_that_the_cell_still_shows_is_elsewhere(self):
        s = seed()
        cell, row = self._cell_with_a_row_elsewhere(s)
        assert (
            sub_line_warn_reason(cell, backing_sets=(), elsewhere_sets=(row,))
            == "elsewhere"
        )

    def test_no_row_anywhere_is_unlogged(self):
        s = seed()
        cell = sub_line(s.squat, "225 x 5", line=1)
        assert (
            sub_line_warn_reason(cell, backing_sets=(), elsewhere_sets=()) == "unlogged"
        )

    def test_elsewhere_sets_not_passed_defaults_to_the_safe_unlogged(self):
        s = seed()
        # `row` really does back this cell elsewhere, but the caller doesn't
        # say so -- exactly a caller that hasn't been taught about
        # `elsewhere_sets` (none exists in this codebase today, per the
        # function's own docstring). The fallback still has to be the reason
        # that keeps today's repost, not the one that suppresses it.
        cell, _row = self._cell_with_a_row_elsewhere(s)
        assert sub_line_warn_reason(cell, backing_sets=()) == "unlogged"
