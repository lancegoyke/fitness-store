"""#579 — the coach's results screen must share the newest-``SessionLog`` rule.

#568 made six reads of "the athlete's newest ``SessionLog`` for this
(session, athlete) pair" agree, tie-breaking on ``-pk`` after ``-created_at``.
``presenters.session_results`` — the coach's results screen — turned out to be
a seventh such read, and it was missed: it ordered by ``-date, -created_at``
instead. ``SessionLog.date`` is athlete-supplied (``views.athlete_log_session``
accepts an explicit date from the payload and only stamps today when none is
given), so a log written LATER but carrying an EARLIER workout date lost to an
older log with a later date — and two logs sharing ``(date, created_at)``
sorted nondeterministically, with no ``-pk`` tie-break at all.

#579's fix is ``models.newest_session_logs``, the one selector every one of
these reads (including ``session_results``) now shares. This file pins the two
cases that used to make the coach's results screen and the athlete's own
logger page disagree about which log is current — both against real,
observable output (the sets each presenter renders), not just a log pk.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import seed

pytestmark = pytest.mark.django_db


def _athlete_set_rows(ctx, prescription):
    """The non-blank (reps, load) pairs ``athlete_session`` shows for one row."""
    row = next(e for e in ctx["exercises"] if e["id"] == prescription.pk)
    return [(r["reps"], r["load"]) for r in row["set_rows"] if r["reps"] or r["load"]]


def _coach_logged_label(ctx, name):
    """The "logged" summary ``session_results`` shows for one row by name."""
    row = next(r for r in ctx["rows"] if r["name"] == name)
    return row["logged"]


class TestSessionResultsAgreesWithAthletePage:
    """Both presenters must read the same ``SessionLog`` for one (session, athlete)."""

    def test_newest_created_at_wins_over_an_older_log_with_a_newer_date(self, client):
        """Log B is written LAST but dated EARLIER than log A — B must win.

        On ``main`` ``session_results`` orders by ``-date`` first, so it reads
        log A (today's date, 100x5); ``athlete_session`` already orders by
        ``-created_at`` (#568), so it reads log B (140x3, written later). The
        two presenters disagree about which set exists. #579 makes
        ``session_results`` read log B too.
        """
        s = seed()
        now = timezone.now()
        today = timezone.localdate()

        # Log A: dated TODAY, but the OLDER write — holds 100 x 5.
        log_a = SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=today,
        )
        LoggedSetFactory(
            session_log=log_a,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="100",
        )
        SessionLog.objects.filter(pk=log_a.pk).update(
            created_at=now - timedelta(hours=2)
        )

        # Log B: dated YESTERDAY, but the NEWER write — holds 140 x 3.
        log_b = SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=today - timedelta(days=1),
        )
        LoggedSetFactory(
            session_log=log_b,
            prescription=s.squat,
            set_number=1,
            reps="3",
            load="140",
        )
        SessionLog.objects.filter(pk=log_b.pk).update(created_at=now)

        athlete_ctx = presenters.athlete_session(s.session, s.athlete)
        assert _athlete_set_rows(athlete_ctx, s.squat) == [("3", "140")]

        coach_ctx = presenters.session_results(s.session)
        assert _coach_logged_label(coach_ctx, "Box Squat") == "1×3 @ 140 kg"

    def test_ties_on_created_at_break_the_same_way_on_both_surfaces(self, client):
        """Two DONE logs sharing ``created_at`` must resolve to the higher pk.

        On ``main`` there is no ``-pk`` tie-break in ``session_results`` at
        all, so which of the two logs it picks is up to however the database
        happens to order an unbroken tie — it could match ``athlete_session``
        by chance, or it could not. #579 makes both order the same
        deterministic way.
        """
        s = seed()
        tied_at = timezone.now()
        today = timezone.localdate()

        log_low = SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=today,
        )
        LoggedSetFactory(
            session_log=log_low,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="100",
        )

        log_high = SessionLogFactory(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=today,
        )
        LoggedSetFactory(
            session_log=log_high,
            prescription=s.squat,
            set_number=1,
            reps="3",
            load="140",
        )

        assert log_high.pk > log_low.pk
        SessionLog.objects.filter(pk__in=[log_low.pk, log_high.pk]).update(
            created_at=tied_at
        )

        athlete_ctx = presenters.athlete_session(s.session, s.athlete)
        assert _athlete_set_rows(athlete_ctx, s.squat) == [("3", "140")]

        coach_ctx = presenters.session_results(s.session)
        assert _coach_logged_label(coach_ctx, "Box Squat") == "1×3 @ 140 kg"
