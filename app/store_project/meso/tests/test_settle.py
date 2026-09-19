"""24-hour settle sweep (5b, ``settle.py``) — flips a quiet PENDING log to DONE.

5a made the athlete's typed "what you did" sub-lines create a PENDING
``SessionLog`` with structured ``LoggedSet`` rows (parse-at-commit,
``athlete_cell_write``), alongside the pre-existing structured "Save
progress"/"Log session" path (``athlete_log_session``). Lots of Meso reads are
DONE-only (adherence, persisted 1RM, coach results, agent grounding), so a
typed-then-abandoned session never counted anywhere but the athlete's own
page. The sweep here promotes a PENDING log with at least one logged set (of
either origin) to DONE once it has gone quiet — no athlete activity — for
``MESO_SETTLE_QUIET_HOURS``.

Reuses ``seed()``/``write_cell()``/``log_post()``/``the_log()``/``sub_cell()``
from ``test_parse_at_commit.py`` — this is the same write-path fixture, just
aged past the quiet window with ``set_activity()`` below.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from store_project.meso import one_rm as meso_one_rm
from store_project.meso import settle
from store_project.meso import tasks
from store_project.meso import tour
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

QUIET = timedelta(hours=24)  # the default MESO_SETTLE_QUIET_HOURS


def set_activity(log, when):
    """Backdate ``log.last_activity_at`` to ``when`` via a queryset UPDATE.

    Deliberately not ``log.save()`` — this must not disturb any other field
    (mirrors why the athlete-write bump itself is a queryset ``.update()``).
    """
    SessionLog.objects.filter(pk=log.pk).update(last_activity_at=when)
    log.refresh_from_db(fields=["last_activity_at"])
    return log


def quiet_since(hours_ago=None):
    """A ``last_activity_at`` stamp comfortably past the quiet window."""
    return timezone.now() - QUIET - timedelta(hours=hours_ago or 0, minutes=1)


def _self_seed(coach):
    """A self-link coach + a loggable session on their own plan (no client)."""
    CoachProfile.objects.get_or_create(user=coach)
    link = CoachAthlete.add_self(coach)
    plan = PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    week = WeekFactory(mesocycle=meso, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower")
    squat = presc(session, name="Box Squat", sets="3", reps="6", load="70", rpe="7")
    return session, squat


# -- what settles ------------------------------------------------------------


class TestSettleableLogs:
    def test_settles_a_quiet_parsed_pending_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.PENDING
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 1
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_leaves_a_log_quiet_less_than_the_period(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, timezone.now() - QUIET + timedelta(minutes=1))

        assert settle.settle_quiet_logs() == 0
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING

    def test_boundary_exactly_at_cutoff_settles(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        now = timezone.now()
        set_activity(log, now - QUIET)  # exactly the cutoff

        assert settle.settle_quiet_logs(now=now) == 1
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_settles_a_structured_only_save_progress_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "100",
                        "rpe": "8",
                    }
                ],
            },
        )
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.PENDING
        assert log.sets.filter(source_line__isnull=True).exists()
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 1
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_never_settles_a_zero_set_notes_only_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
            client,
            s.session,
            {"status": "pending", "notes": "felt off, skipped everything", "sets": []},
        )
        log = the_log(s.session, s.athlete)
        assert log.sets.count() == 0
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 0
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING

    def test_never_touches_done_logs(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
            client,
            s.session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "100",
                    }
                ],
            },
        )
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.DONE
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 0
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_never_settles_a_non_newest_duplicate(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        newest = the_log(s.session, s.athlete)
        set_activity(newest, quiet_since())

        # An older duplicate for the same (session, athlete) pair, built
        # directly with the ORM (backdated `created_at`) — the write paths'
        # own session lock prevents this from happening in practice; this
        # pins the sweep's OWN newest-only rule regardless of how a duplicate
        # got there.
        older = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        LoggedSetFactory(
            session_log=older, prescription=s.squat, set_number=1, reps="5", load="90"
        )
        SessionLog.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=1),
            last_activity_at=quiet_since(),
        )

        assert settle.settle_quiet_logs() == 1
        newest.refresh_from_db()
        older.refresh_from_db()
        assert newest.status == SessionLog.Status.DONE
        assert older.status == SessionLog.Status.PENDING


# -- what a settle leaves untouched ------------------------------------------


class TestSettleLeavesDataAlone:
    def test_does_not_change_date_notes_activity_or_any_logged_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
            client,
            s.session,
            {
                "status": "pending",
                "notes": "left knee tight",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "100",
                        "rpe": "8",
                    }
                ],
            },
        )
        log = the_log(s.session, s.athlete)
        stamp = quiet_since()
        set_activity(log, stamp)
        before_date = log.date
        before_notes = log.notes
        before_sets = [
            (row.pk, row.prescription_id, row.set_number, row.reps, row.load, row.rpe)
            for row in log.sets.all()
        ]

        assert settle.settle_quiet_logs() == 1

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE
        assert log.date == before_date
        assert log.notes == before_notes
        assert log.last_activity_at == stamp
        after_sets = [
            (row.pk, row.prescription_id, row.set_number, row.reps, row.load, row.rpe)
            for row in log.sets.all()
        ]
        assert after_sets == before_sets


# -- 1RM refresh --------------------------------------------------------------


class TestSettleRefreshesOneRm:
    def test_refreshes_the_persisted_one_rm_for_a_typed_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        key = meso_one_rm.key_str(s.squat.exercise_id, s.squat.name)
        assert not AthleteOneRm.objects.filter(athlete=s.athlete, key=key).exists()
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 1

        row = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert row.value == Decimal("116.67")  # Epley(100, 5)
        assert row.source == AthleteOneRm.Source.LOGGED

    def test_leaves_a_same_unit_manual_one_rm_untouched(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        key = meso_one_rm.key_str(s.squat.exercise_id, s.squat.name)
        AthleteOneRm.objects.update_or_create(
            athlete=s.athlete,
            key=key,
            defaults={
                "exercise_id": s.squat.exercise_id,
                "name": s.squat.name,
                "value": Decimal("999.99"),
                "unit": s.plan.unit,
                "source": AthleteOneRm.Source.MANUAL,
            },
        )
        set_activity(log, quiet_since())

        settle.settle_quiet_logs()

        row = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert row.source == AthleteOneRm.Source.MANUAL
        assert row.value == Decimal("999.99")


# -- tour side effect ---------------------------------------------------------


class TestSettleAdvancesTour:
    def test_advances_the_self_results_step_when_parked_there(self):
        coach = UserFactory()
        session, squat = _self_seed(coach)
        log = SessionLog.objects.create(
            session=session,
            athlete=coach,
            status=SessionLog.Status.PENDING,
            date=timezone.localdate(),
        )
        LoggedSetFactory(
            session_log=log, prescription=squat, set_number=1, reps="5", load="100"
        )
        set_activity(log, quiet_since())
        tour.set_step(coach.coach_profile, 4)  # results

        assert settle.settle_quiet_logs() == 1
        assert tour.tour_status(coach)["step"] == 5  # agent

    def test_does_nothing_for_a_normal_athlete(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        # A plain athlete has no CoachProfile at all — must not raise.
        assert settle.settle_quiet_logs() == 1
        assert not CoachProfile.objects.filter(user=s.athlete).exists()


# -- idempotency + fault isolation --------------------------------------------


class TestIdempotentAndFaultIsolated:
    def test_a_second_sweep_settles_nothing_new(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        assert settle.settle_quiet_logs() == 1
        assert settle.settle_quiet_logs() == 0

    def test_one_failing_log_does_not_block_the_rest(self, client, monkeypatch):
        good = seed()
        bad = seed()
        client.force_login(good.athlete)
        write_cell(client, good.session, good.squat, 1, "100 x 5")
        good_log = the_log(good.session, good.athlete)
        set_activity(good_log, quiet_since())

        client.force_login(bad.athlete)
        write_cell(client, bad.session, bad.squat, 1, "100 x 5")
        bad_log = the_log(bad.session, bad.athlete)
        set_activity(bad_log, quiet_since())

        real_refresh = meso_one_rm.refresh_one_rms

        def flaky(athlete, prescriptions, unit):
            if athlete.pk == bad.athlete.pk:
                raise RuntimeError("boom")
            return real_refresh(athlete, prescriptions, unit)

        monkeypatch.setattr(settle.meso_one_rm, "refresh_one_rms", flaky)

        assert settle.settle_quiet_logs() == 1

        good_log.refresh_from_db()
        bad_log.refresh_from_db()
        assert good_log.status == SessionLog.Status.DONE
        # The failing log's whole transaction rolled back — it never flipped.
        assert bad_log.status == SessionLog.Status.PENDING


# -- under-lock re-check ------------------------------------------------------


class TestUnderLockRecheck:
    """``settle_log`` re-verifies every rule-1 condition AFTER taking the lock.

    Each test selects a candidate the normal way, then mutates it exactly as a
    concurrent blur/save/history-restore could between candidate selection and
    the lock being acquired, then calls ``settle_log`` directly and asserts it
    backs off cleanly rather than settling stale state.
    """

    def test_last_activity_bumped_after_selection_stays_pending(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        cutoff = timezone.now() - QUIET
        set_activity(log, cutoff - timedelta(minutes=1))
        assert list(settle.settleable_logs(cutoff).values_list("pk", flat=True)) == [
            log.pk
        ]

        set_activity(log, timezone.now())  # a blur lands after selection

        assert settle.settle_log(log.pk, cutoff=cutoff) is False
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING

    def test_log_became_done_after_selection_stays_done(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        cutoff = timezone.now() - QUIET
        set_activity(log, cutoff - timedelta(minutes=1))

        SessionLog.objects.filter(pk=log.pk).update(status=SessionLog.Status.DONE)

        assert settle.settle_log(log.pk, cutoff=cutoff) is False
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_a_newer_log_appearing_after_selection_stops_the_settle(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        cutoff = timezone.now() - QUIET
        set_activity(log, cutoff - timedelta(minutes=1))

        SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )

        assert settle.settle_log(log.pk, cutoff=cutoff) is False
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING

    def test_sets_deleted_after_selection_stops_the_settle(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        cutoff = timezone.now() - QUIET
        set_activity(log, cutoff - timedelta(minutes=1))

        log.sets.all().delete()

        assert settle.settle_log(log.pk, cutoff=cutoff) is False
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING


# -- end-to-end with the real write paths -------------------------------------


class TestEndToEndWithWritePaths:
    def test_a_blur_after_settle_keeps_done_and_refreshes_the_one_rm(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        assert settle.settle_quiet_logs() == 1
        key = meso_one_rm.key_str(s.squat.exercise_id, s.squat.name)
        before = AthleteOneRm.objects.get(athlete=s.athlete, key=key)

        write_cell(client, s.session, s.squat, 1, "120 x 5")

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE
        after = AthleteOneRm.objects.get(pk=before.pk)
        assert after.value > before.value

    def test_a_pending_save_after_settle_keeps_it_done(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        assert settle.settle_quiet_logs() == 1

        log_post(client, s.session, {"status": "pending", "sets": []})

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE


# -- last_activity_at bumps on the athlete write paths ------------------------


class TestActivityBumps:
    def test_a_real_cell_edit_bumps_last_activity(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        old = timezone.now() - timedelta(hours=10)
        set_activity(log, old)

        write_cell(client, s.session, s.squat, 1, "120 x 5")

        log.refresh_from_db()
        assert log.last_activity_at > old + timedelta(hours=9)

    def test_a_no_change_reblur_does_not_bump(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        old = timezone.now() - timedelta(hours=10)
        set_activity(log, old)

        write_cell(client, s.session, s.squat, 1, "100 x 5")  # identical text

        log.refresh_from_db()
        assert log.last_activity_at == old

    def test_an_unchanged_reblur_that_now_derives_a_set_bumps(self, client):
        # Text typed while the row was skipped saves no set. Once the coach
        # un-skips the row, re-blurring the SAME text creates one — a change
        # to the log's data, so the quiet clock has to restart.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")  # creates the log
        s.rdl.skipped = True
        s.rdl.save(update_fields=["skipped"])
        write_cell(client, s.session, s.rdl, 1, "80 x 8")  # text only, no set
        log = the_log(s.session, s.athlete)
        assert log.sets.filter(prescription=s.rdl).count() == 0
        old = timezone.now() - timedelta(hours=30)
        set_activity(log, old)
        s.rdl.skipped = False
        s.rdl.save(update_fields=["skipped"])

        write_cell(client, s.session, s.rdl, 1, "80 x 8")  # identical text

        log.refresh_from_db()
        assert log.sets.filter(prescription=s.rdl).count() == 1
        assert log.last_activity_at > old + timedelta(hours=29)
        assert settle.settle_quiet_logs() == 0

    def test_an_untouched_coach_line_blur_does_not_bump(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")  # creates the log
        log = the_log(s.session, s.athlete)
        old = timezone.now() - timedelta(hours=10)
        set_activity(log, old)
        sub_line(s.rdl, "coach cue: brace harder", line=2, athlete_authored=False)

        # The athlete's page posts the coach's cue back unchanged on blur.
        write_cell(client, s.session, s.rdl, 2, "coach cue: brace harder")

        log.refresh_from_db()
        assert log.last_activity_at == old

    def test_log_session_bumps_for_both_pending_and_done(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {"status": "pending", "sets": []})
        log = the_log(s.session, s.athlete)
        old = timezone.now() - timedelta(hours=10)

        set_activity(log, old)
        log_post(client, s.session, {"status": "pending", "sets": []})
        log.refresh_from_db()
        assert log.last_activity_at > old + timedelta(hours=9)

        set_activity(log, old)
        log_post(client, s.session, {"status": "done", "sets": []})
        log.refresh_from_db()
        assert log.last_activity_at > old + timedelta(hours=9)

    def test_a_brand_new_logs_last_activity_is_now(self, client):
        s = seed()
        client.force_login(s.athlete)
        before = timezone.now()
        log_post(client, s.session, {"status": "pending", "sets": []})
        after = timezone.now()

        log = the_log(s.session, s.athlete)
        assert before <= log.last_activity_at <= after


# -- management command + task wrapper ----------------------------------------


class TestManagementCommandAndTask:
    def test_dry_run_reports_the_count_and_changes_nothing(self, client, capsys):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        call_command("meso_settle_logs", "--dry-run")

        captured = capsys.readouterr()
        assert "1" in captured.out
        log.refresh_from_db()
        assert log.status == SessionLog.Status.PENDING

    def test_the_real_run_settles(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        call_command("meso_settle_logs")

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_task_wrapper_settles(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        tasks.settle_logs()

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE


# -- visible surfaces after a settle ------------------------------------------


class TestVisibleSurfacesAfterSettle:
    def test_athlete_session_page_shows_logged(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        settle.settle_quiet_logs()

        body = client.get(
            reverse("meso:athlete_session", kwargs={"pk": s.session.pk})
        ).content.decode()
        assert "Logged" in body

    def test_coach_results_page_shows_logged_session(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        settle.settle_quiet_logs()

        client.force_login(s.coach)
        body = client.get(
            reverse("meso:results_session", kwargs={"session_id": s.session.pk})
        ).content.decode()
        assert "Logged session" in body

    def test_first_log_hint_hides_after_a_settle(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)

        body = client.get(
            reverse("meso:athlete_session", kwargs={"pk": s.session.pk})
        ).content.decode()
        assert 'data-coachmark-key="firstlog-session"' in body

        set_activity(log, quiet_since())
        settle.settle_quiet_logs()

        body = client.get(
            reverse("meso:athlete_session", kwargs={"pk": s.session.pk})
        ).content.decode()
        assert 'data-coachmark-key="firstlog-session"' not in body
