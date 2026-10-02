"""#578 stage 3 / #575 — one "performance history" selector.

The rule (decided by the product owner, 2026-10-02), pinned here from every
read that counts an athlete's logged sets:

- Case A counts: a ``LoggedSet`` on a session/week/exercise slot the coach
  later soft-deleted STILL counts.
- Case B doesn't: a ``LoggedSet`` on a ``SessionLog`` that is not the newest
  log of its ``(session, athlete)`` pair (``-created_at, -pk``) counts toward
  NO read.
- The newest log is chosen over ALL the pair's logs regardless of status; a
  caller's status filter (DONE, ...) is applied on top.

Tests marked GUARD pass on main too; they exist so the selector can't quietly
start filtering ``deleted_at`` (mutation-proved when the change landed).
"""

import datetime
import json

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from store_project.meso import adherence
from store_project.meso import one_rm
from store_project.meso import personal_records
from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import seed

pytestmark = pytest.mark.django_db

DONE = SessionLog.Status.DONE
PENDING = SessionLog.Status.PENDING


def make_log(session, athlete, *, status=DONE, age_days=0, date=None):
    """A ``SessionLog`` created directly, ``age_days`` older than now.

    ``created_at`` is ``auto_now_add``, so the age is applied with an
    ``.update()`` to make "newest" unambiguous rather than a timestamp tie.
    """
    log = SessionLog.objects.create(
        session=session, athlete=athlete, status=status, date=date
    )
    SessionLog.objects.filter(pk=log.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=age_days)
    )
    log.refresh_from_db()
    return log


def add_set(log, cell, *, load, reps="5", source_line=None):
    return LoggedSet.objects.create(
        session_log=log,
        prescription=cell,
        source_line=source_line,
        set_number=1,
        reps=reps,
        load=load,
        rpe="",
    )


def squat_key(s):
    return one_rm.key_str(s.squat.exercise_id, s.squat.name)


def soft_delete_day(session):
    """Soft-delete the day the way the designer does (slot -> its sessions)."""
    session.session_slot.soft_delete()


def sub_line_ctx(ctx, s):
    squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
    return next(line for line in squat_ctx["sub_lines"] if line["line"] == 1)


# -- 1-5: a stranded older log is now impossible (#699) ----------------------


class TestOneLogPerPairIsEnforced:
    def test_the_database_refuses_a_second_log_for_a_pair(self):
        # Replaces the old "stranded older log counts nowhere" cases: that
        # state can no longer be built, so the guarantee is the DB's.
        s = seed()
        make_log(s.session, s.athlete)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_log(s.session, s.athlete, age_days=2)


# -- 6: GUARD, case A --------------------------------------------------------


class TestSoftDeletedDayStillCounts:
    """GUARD: a set on a soft-deleted day feeds every performance-history read."""

    @pytest.fixture
    def deleted_day(self):
        s = seed()
        log = make_log(s.session, s.athlete, date=timezone.localdate())
        add_set(log, s.squat, load="150")
        soft_delete_day(s.session)
        s.session.refresh_from_db()
        assert s.session.deleted_at is not None
        return s

    def test_one_rm(self, deleted_day):
        s = deleted_day
        assert one_rm.derive_one_rm_values(s.athlete) == {
            squat_key(s): one_rm.epley_one_rm("150", "5")
        }

    def test_personal_records(self, deleted_day):
        s = deleted_day
        prs = personal_records.personal_records(s.athlete, unit=s.plan.unit)
        assert prs[squat_key(s)].load == "150"

    def test_last_logged_label(self, deleted_day):
        s = deleted_day
        labels = serializers.last_logged_labels(s.plan, [s.squat], s.plan.unit)
        assert "150" in labels[s.squat.pk]

    def test_recent_logs(self, deleted_day):
        s = deleted_day
        assert len(serializers.serialize_recent_logs(s.plan)) == 1
        assert adherence.link_last_trained(s.rel) is not None
        assert adherence.link_session_count(s.rel) == 1
        assert len(adherence.recent_logs(s.coach)) == 1


# -- 7 / 8: #572 "elsewhere" -------------------------------------------------


def _moved_exercise(client):
    """A squat sub-line typed on day 1, then the coach moves the squat to day 2.

    Returns ``(s, day2, cell)`` with NO LoggedSet yet; each test files the
    backing row on the day-1 log it wants.
    """
    s = seed()
    day2 = day(s.week, day_number=2, name="Upper", bias="Push")
    cell = sub_line(s.squat, "225 x 5", line=1, athlete_authored=True)
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
    return s, day2, cell


def _reasons(s, day2, cell):
    """The warn reason on BOTH surfaces: the page render and the blur response."""
    render = sub_line_ctx(presenters.athlete_session(day2, s.athlete), s)
    fresh_line_zero = Prescription.objects.get(pk=s.squat.pk)
    blur = views._cell_warn_reason_or_blank(
        cell, fresh_line_zero, session=day2, athlete=s.athlete
    )
    return render["warn_reason"], blur


class TestElsewhereReadsTheSelector:
    def test_guard_a_row_on_a_soft_deleted_day_still_reads_as_elsewhere(self, client):
        """GUARD (#572 case A): the row still counts, so a re-post would double it."""
        s, day2, cell = _moved_exercise(client)
        log = make_log(s.session, s.athlete)
        add_set(log, s.squat, load="225", source_line=cell)
        soft_delete_day(s.session)

        assert _reasons(s, day2, cell) == ("elsewhere", "elsewhere")

    def test_a_row_on_the_newest_log_reads_as_elsewhere(self, client):
        s, day2, cell = _moved_exercise(client)
        newest = make_log(s.session, s.athlete, age_days=0)
        add_set(newest, s.squat, load="225", source_line=cell)

        assert _reasons(s, day2, cell) == ("elsewhere", "elsewhere")


# -- 9: plan-shaped reads skip soft-deleted sessions -------------------------


class TestPlanShapedReadsIgnoreDeletedSessions:
    def _two_days(self):
        s = seed()
        day2 = day(s.week, day_number=2, name="Upper", bias="Push")
        presc(day2, name="Bench", order=0)
        # The soft-deleted day carries the LATER workout date, so it wins on
        # main.
        today = timezone.localdate()
        make_log(s.session, s.athlete, date=today)
        live = make_log(
            day2, s.athlete, age_days=1, date=today - datetime.timedelta(days=1)
        )
        soft_delete_day(s.session)
        return s, day2, live

    def test_profile_results_fall_back_to_the_live_session(self):
        s, day2, _ = self._two_days()
        summary = presenters._profile_results(s.rel)
        assert summary is not None
        assert summary["session_id"] == day2.pk

    def test_profile_results_none_when_only_deleted_sessions_are_done(self):
        s = seed()
        make_log(s.session, s.athlete, date=timezone.localdate())
        soft_delete_day(s.session)
        assert presenters._profile_results(s.rel) is None

    def test_coach_latest_logged_session_falls_back_to_the_live_session(self):
        s, day2, _ = self._two_days()
        assert views._coach_latest_logged_session(s.coach) == day2

    def test_coach_latest_logged_session_none_when_only_deleted(self):
        s = seed()
        make_log(s.session, s.athlete, date=timezone.localdate())
        soft_delete_day(s.session)
        assert views._coach_latest_logged_session(s.coach) is None


# -- 10: one query -----------------------------------------------------------


class TestSelectorShape:
    def test_performance_history_is_one_query(self, django_assert_num_queries):
        s = seed()
        new = make_log(s.session, s.athlete, age_days=0)
        kept = add_set(new, s.squat, load="100")

        with django_assert_num_queries(1):
            rows = list(LoggedSet.objects.performance_history(s.athlete))
        assert [r.pk for r in rows] == [kept.pk]

    def test_other_athletes_are_not_included(self):
        s = seed()
        log = make_log(s.session, s.athlete)
        add_set(log, s.squat, load="100")
        assert not LoggedSet.objects.performance_history(s.coach).exists()
