"""Units & RPE/%1RM slice (S2) — persisted, auto-derived estimated 1RM.

The deferred follow-up to Phase 2b: the athlete's estimated 1RM lived only in
the browser's localStorage (per-device, invisible to the coach). This slice
promotes it to a real ``AthleteOneRm`` row, **derived from the athlete's logged
history** (the best Epley estimate per lift), so it survives a device change,
powers the logger's suggested load on any device, and is visible to the coach in
the designer when prescribing a %1RM.

These tests pin: the lift-identity key + uniqueness, the Epley derivation
(matching ``meso_athlete.js`` exactly), the refresh-on-log write path, the read
helper, the log-endpoint integration, the presenter/serializer threading, and
the backfill of existing history. See ``docs/archive/meso/one-rm-plan.md``.
"""

import datetime
import importlib
import json
from decimal import Decimal

import pytest
from django.apps import apps as global_apps
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from store_project.meso import one_rm as meso_one_rm
from store_project.meso import presenters
from store_project.meso.factories import AthleteOneRmFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import LoadType
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.serializers import serialize_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# -- fixtures / helpers ----------------------------------------------------


def make_session(athlete, *, coach=None, unit=Unit.KILOGRAMS, prescriptions=()):
    """A delivered-shape plan → week → session with the given prescription specs."""
    coach = coach or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE, unit=unit)
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    week = WeekFactory(
        mesocycle=meso, index=1, is_current=True, delivered_at=timezone.now()
    )
    session = SessionFactory(week=week, day_number=1, name="Lower")
    presc = [
        ExercisePrescriptionFactory(session=session, order=i, **spec)
        for i, spec in enumerate(prescriptions)
    ]
    return plan, session, presc


def log_session(athlete, session, rows, *, status=SessionLog.Status.DONE, date=None):
    """A ``SessionLog`` + ``LoggedSet`` rows. ``rows``: (presc, set_no, reps, load, rpe)."""
    log = SessionLogFactory(
        session=session,
        athlete=athlete,
        status=status,
        date=date or timezone.localdate(),
    )
    for presc, set_number, reps, load, rpe in rows:
        LoggedSetFactory(
            session_log=log,
            prescription=presc,
            set_number=set_number,
            reps=reps,
            load=load,
            rpe=rpe,
        )
    return log


# -- the identity key + uniqueness -----------------------------------------


class TestKeyAndUniqueness:
    def test_free_text_key_is_normalized_name(self):
        row = AthleteOneRmFactory(exercise=None, name="Back Squat")
        assert row.key == "name:back squat"

    def test_catalog_key_is_fk(self):
        from store_project.exercises.factories import ExerciseFactory

        ex = ExerciseFactory()
        row = AthleteOneRmFactory(exercise=ex, name="whatever")
        assert row.key == f"id:{ex.pk}"

    def test_two_free_text_lifts_for_one_athlete_coexist(self):
        athlete = UserFactory()
        AthleteOneRmFactory(athlete=athlete, name="Back Squat")
        AthleteOneRmFactory(athlete=athlete, name="Bench Press")
        assert AthleteOneRm.objects.filter(athlete=athlete).count() == 2

    def test_same_lift_same_athlete_violates_unique(self):
        athlete = UserFactory()
        AthleteOneRmFactory(athlete=athlete, name="Back Squat")
        with pytest.raises(IntegrityError):
            AthleteOneRmFactory(athlete=athlete, name="back squat")  # case-folded


# -- Epley (must mirror meso_athlete.js epleyOneRm) ------------------------


class TestEpley:
    def test_single_rep_is_the_load(self):
        assert meso_one_rm.epley_one_rm("140", "1") == 140

    def test_multi_rep_uses_epley(self):
        # 110 × 3 → 110 × (1 + 3/30) = 121
        assert meso_one_rm.epley_one_rm("110", "3") == pytest.approx(121.0)

    @pytest.mark.parametrize(
        "load,reps",
        [
            ("BW", "5"),
            ("100", "AMRAP"),
            ("", "5"),
            ("0", "5"),
            ("-50", "3"),
            ("100", "0"),
        ],
    )
    def test_non_numeric_or_out_of_range_is_none(self, load, reps):
        assert meso_one_rm.epley_one_rm(load, reps) is None


# -- derive_one_rm_values --------------------------------------------------


class TestDeriveOneRmValues:
    def test_best_estimate_across_completed_logs(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "5", "100", "8")])  # → 116.67
        log_session(athlete, session, [(squat, 1, "3", "110", "9")])  # → 121 (best)
        values = meso_one_rm.derive_one_rm_values(athlete)
        assert values == {"name:back squat": pytest.approx(121.0)}

    def test_pending_log_is_ignored(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "3", "200", "9")],
            status=SessionLog.Status.PENDING,
        )
        assert meso_one_rm.derive_one_rm_values(athlete) == {}

    def test_non_numeric_sets_are_skipped(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "AMRAP", "BW", "9")])
        assert meso_one_rm.derive_one_rm_values(athlete) == {}

    def test_keys_filter_restricts_the_scan(self):
        athlete = UserFactory()
        _, session, (squat, bench) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}, {"name": "Bench Press"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "1", "150", "9"), (bench, 1, "1", "100", "9")],
        )
        values = meso_one_rm.derive_one_rm_values(athlete, keys={"name:bench press"})
        assert values == {"name:bench press": pytest.approx(100.0)}

    def test_scoped_to_the_athlete(self):
        athlete, other = UserFactory(), UserFactory()
        _, session, (squat,) = make_session(
            other, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(other, session, [(squat, 1, "1", "999", "9")])
        assert meso_one_rm.derive_one_rm_values(athlete) == {}

    def test_unit_scopes_the_scan(self):
        # The same lift logged in a kg plan and a lb plan: each unit derives only
        # from its own logs (a bare logged load can't be pooled across units).
        athlete = UserFactory()
        _, kg_session, (kg_squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        _, lb_session, (lb_squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, kg_session, [(kg_squat, 1, "1", "150", "9")])
        log_session(athlete, lb_session, [(lb_squat, 1, "1", "300", "9")])
        assert meso_one_rm.derive_one_rm_values(athlete, unit=Unit.KILOGRAMS) == {
            "name:back squat": pytest.approx(150.0)
        }
        assert meso_one_rm.derive_one_rm_values(athlete, unit=Unit.POUNDS) == {
            "name:back squat": pytest.approx(300.0)
        }


# -- refresh_one_rms -------------------------------------------------------


class TestRefreshOneRms:
    def test_creates_a_row_from_logs(self):
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "140", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("140.00")
        assert row.unit == Unit.POUNDS
        assert row.name == "Back Squat"

    def test_updates_an_existing_row_upward(self):
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "100", "8")])
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        log_session(athlete, session, [(squat, 1, "3", "130", "9")])  # → 143
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("143.00")
        assert AthleteOneRm.objects.filter(athlete=athlete).count() == 1

    def test_lift_with_no_usable_set_creates_nothing(self):
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "AMRAP", "BW", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_clears_a_stale_row_when_derivation_disappears(self):
        # A lift had a row; the athlete edits the log so no numeric set remains.
        # The now-unsupported estimate must be cleared, not left showing.
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log = log_session(athlete, session, [(squat, 1, "1", "150", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        assert AthleteOneRm.objects.filter(athlete=athlete).exists()
        # The athlete edits the log so the set is no longer numeric.
        log.sets.all().delete()
        LoggedSetFactory(
            session_log=log, prescription=squat, set_number=1, reps="AMRAP", load="BW"
        )
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_stale_clear_leaves_a_row_in_the_other_unit(self):
        # Clearing on a None kg derivation must not drop a valid lb row.
        athlete = UserFactory()
        kg_plan, _, (kg_squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        AthleteOneRmFactory(
            athlete=athlete, name="Back Squat", value=Decimal("300"), unit=Unit.POUNDS
        )
        # No kg logs exist, so the kg derivation is empty — but the lb row stays.
        meso_one_rm.refresh_one_rms(athlete, [kg_squat], kg_plan.unit)
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.unit == Unit.POUNDS
        assert row.value == Decimal("300")

    def test_refresh_pools_only_same_unit_logs(self):
        # A lb log of the same lift must not pollute the kg estimate.
        athlete = UserFactory()
        kg_plan, kg_session, (kg_squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        _, lb_session, (lb_squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, kg_session, [(kg_squat, 1, "1", "150", "9")])
        log_session(athlete, lb_session, [(lb_squat, 1, "1", "300", "9")])
        meso_one_rm.refresh_one_rms(athlete, [kg_squat], kg_plan.unit)
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("150.00")
        assert row.unit == Unit.KILOGRAMS

    def test_absurd_load_is_skipped_not_an_overflow(self):
        # A fat-fingered logged load whose estimate won't fit Decimal(7, 2) must
        # not raise (which would roll back the whole log) — it's simply skipped.
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "1000000", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], plan.unit)
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()


# -- one_rm_values (read helper) -------------------------------------------


class TestOneRmValues:
    def test_maps_prescription_pk_to_stored_row(self):
        athlete = UserFactory()
        _, _, (squat,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("150"))
        values = meso_one_rm.one_rm_values(athlete, [squat], Unit.KILOGRAMS)
        assert values[squat.pk].value == Decimal("150")

    def test_same_lift_surfaces_against_every_prescription(self):
        # Two prescriptions of the same free-text lift (e.g. across weeks) share one
        # identity, so both map to the single stored 1RM.
        athlete = UserFactory()
        _, _, (wk1,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        _, _, (wk2,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("150"))
        values = meso_one_rm.one_rm_values(athlete, [wk1, wk2], Unit.KILOGRAMS)
        assert values[wk1.pk].pk == values[wk2.pk].pk

    def test_absent_when_no_row(self):
        athlete = UserFactory()
        _, _, (squat,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        assert meso_one_rm.one_rm_values(athlete, [squat], Unit.KILOGRAMS) == {}

    def test_a_row_in_another_unit_is_not_surfaced(self):
        # A 1RM stored in lb must not be read against a kg plan (it would render as
        # "150 kg" and drive an unsafe suggested load).
        athlete = UserFactory()
        _, _, (squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        AthleteOneRmFactory(
            athlete=athlete, name="Back Squat", value=Decimal("150"), unit=Unit.POUNDS
        )
        assert meso_one_rm.one_rm_values(athlete, [squat], Unit.KILOGRAMS) == {}


# -- log-endpoint integration ----------------------------------------------


def post_log(client, session, payload):
    return client.post(
        reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
        data=json.dumps(payload),
        content_type="application/json",
    )


def post_one_rm(client, session, payload):
    return client.post(
        reverse("meso:athlete_set_one_rm", kwargs={"pk": session.pk}),
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestLogEndpointRefreshesOneRm:
    def test_done_log_writes_the_estimate(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(athlete)
        resp = post_log(
            client,
            session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": squat.pk,
                        "set_number": 1,
                        "reps": "1",
                        "load": "150",
                        "rpe": "9",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("150.00")

    def test_pending_save_does_not_write(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        client.force_login(athlete)
        resp = post_log(
            client,
            session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": squat.pk,
                        "set_number": 1,
                        "reps": "3",
                        "load": "120",
                        "rpe": "8",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_downgrade_to_pending_clears_the_estimate(self, client):
        # A done log creates the row; downgrading the same session back to pending
        # (no completed performance remains) must clear it, not leave it stale.
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        client.force_login(athlete)
        sets = [
            {
                "prescription": squat.pk,
                "set_number": 1,
                "reps": "1",
                "load": "150",
                "rpe": "9",
            }
        ]
        post_log(client, session, {"status": "done", "sets": sets})
        assert AthleteOneRm.objects.filter(athlete=athlete).exists()
        post_log(client, session, {"status": "pending", "sets": sets})
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_relogging_lower_recomputes_downward(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        client.force_login(athlete)
        post_log(
            client,
            session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": squat.pk,
                        "set_number": 1,
                        "reps": "1",
                        "load": "150",
                        "rpe": "9",
                    }
                ],
            },
        )
        # The athlete corrects the log to a lighter set (the single log is replaced),
        # so the recomputed estimate drops with it.
        post_log(
            client,
            session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": squat.pk,
                        "set_number": 1,
                        "reps": "1",
                        "load": "120",
                        "rpe": "8",
                    }
                ],
            },
        )
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("120.00")


# -- presenter / serializer threading --------------------------------------


class TestPresenterThreading:
    def test_athlete_session_exercise_carries_one_rm(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[
                {"name": "Back Squat", "load": "75", "load_type": LoadType.PERCENT}
            ],
        )
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("140"))
        ctx = presenters.athlete_session(session, athlete)
        assert ctx["exercises"][0]["one_rm"] == "140"

    def test_log_payload_carries_one_rm(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[
                {"name": "Back Squat", "load": "75", "load_type": LoadType.PERCENT}
            ],
        )
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("142.5"))
        ctx = presenters.athlete_session(session, athlete)
        payload = presenters.athlete_log_payload(ctx)
        assert payload["exercises"][0]["one_rm"] == "142.5"

    def test_no_row_yields_empty_string(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        ctx = presenters.athlete_session(session, athlete)
        assert ctx["exercises"][0]["one_rm"] == ""


class TestSerializerThreading:
    def test_individual_plan_row_carries_one_rm(self):
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete,
            prescriptions=[
                {"name": "Back Squat", "load": "75", "load_type": LoadType.PERCENT}
            ],
        )
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("140"))
        data = serialize_plan(plan)
        row = data["program"][0]["exercises"][0]
        assert row["one_rm"] == "140"

    def test_group_plan_has_no_one_rm(self):
        # A group plan has no single athlete, so the per-athlete 1RM does not apply.
        group_plan = GroupPlanFactory(status=Plan.Status.ACTIVE)
        meso = MesocycleFactory(plan=group_plan, order=0)
        week = WeekFactory(mesocycle=meso, index=1, is_current=True)
        session = SessionFactory(week=week, day_number=1)
        ExercisePrescriptionFactory(
            session=session, order=0, name="Back Squat", load_type=LoadType.PERCENT
        )
        data = serialize_plan(group_plan)
        assert "one_rm" not in data["program"][0]["exercises"][0]


# -- the backfill data migration -------------------------------------------


class TestBackfillMigration:
    def _backfill(self):
        mig = importlib.import_module(
            "store_project.meso.migrations.0013_backfill_one_rms"
        )
        mig.backfill(global_apps, None)

    def test_backfills_from_existing_completed_logs(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "160", "9")])
        assert not AthleteOneRm.objects.exists()
        self._backfill()
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("160.00")

    def test_backfill_is_idempotent_and_keeps_a_fresher_value(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "160", "9")])
        # A fresher value already exists (e.g. refresh-on-save ran); the one-off
        # pass must not clobber it back down.
        AthleteOneRmFactory(athlete=athlete, name="Back Squat", value=Decimal("180"))
        self._backfill()
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("180")
        assert AthleteOneRm.objects.filter(athlete=athlete).count() == 1

    def test_backfill_uses_the_most_recent_unit_for_mixed_history(self):
        # The same lift logged in a kg plan (older) and a lb plan (newer): the
        # backfill stores the most-recent unit's estimate, never pooling the two.
        athlete = UserFactory()
        _, kg_session, (kg_squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        _, lb_session, (lb_squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            kg_session,
            [(kg_squat, 1, "1", "150", "9")],
            date=datetime.date(2026, 1, 1),
        )
        log_session(
            athlete,
            lb_session,
            [(lb_squat, 1, "1", "300", "9")],
            date=datetime.date(2026, 6, 1),
        )
        self._backfill()
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.unit == Unit.POUNDS
        assert row.value == Decimal("300.00")


# == Phase 2: manual, server-persisted 1RM (the `source` field + endpoint) ===
#
# Phase 1 derived the 1RM from logs; the athlete's *typed* override still lived
# in per-device localStorage. Phase 2 promotes it to a real ``source=manual``
# row: it syncs across devices, is visible to the coach, and survives later logs
# (which only ever *raised* the derived estimate before).


class TestManualSourceField:
    def test_default_source_is_logged(self):
        athlete = UserFactory()
        row = AthleteOneRmFactory(athlete=athlete, name="Back Squat")
        assert row.source == AthleteOneRm.Source.LOGGED

    def test_source_can_be_manual(self):
        athlete = UserFactory()
        row = AthleteOneRmFactory(
            athlete=athlete, name="Back Squat", source=AthleteOneRm.Source.MANUAL
        )
        row.refresh_from_db()
        assert row.source == AthleteOneRm.Source.MANUAL


class TestRefreshSkipsManual:
    def test_refresh_does_not_clobber_a_manual_row(self):
        # A heavier logged set would normally raise the derived estimate; a manual
        # value is the athlete's own number and must be left untouched.
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        AthleteOneRmFactory(
            athlete=athlete,
            name="Back Squat",
            value=Decimal("200"),
            source=AthleteOneRm.Source.MANUAL,
        )
        log_session(athlete, session, [(squat, 1, "1", "150", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], Unit.KILOGRAMS)
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("200")
        assert row.source == AthleteOneRm.Source.MANUAL

    def test_refresh_does_not_clear_a_manual_row_without_logs(self):
        # The stale-clear path (no usable logged set) must also spare a manual row.
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        AthleteOneRmFactory(
            athlete=athlete,
            name="Back Squat",
            value=Decimal("180"),
            source=AthleteOneRm.Source.MANUAL,
        )
        meso_one_rm.refresh_one_rms(athlete, [squat], Unit.KILOGRAMS)
        assert AthleteOneRm.objects.filter(
            athlete=athlete, source=AthleteOneRm.Source.MANUAL
        ).exists()

    def test_manual_in_one_unit_does_not_block_a_logged_estimate_in_another(self):
        # A manual kg row must not suppress the lb estimate when the athlete trains
        # + logs the same lift in a lb plan. The skip is same-unit only (the single
        # row is last-unit-wins; reads are unit-scoped, so the kg row can't surface
        # on a lb plan anyway).
        athlete = UserFactory()
        _, lb_session, (lb_squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        AthleteOneRmFactory(
            athlete=athlete,
            name="Back Squat",
            value=Decimal("150"),
            unit=Unit.KILOGRAMS,
            source=AthleteOneRm.Source.MANUAL,
        )
        log_session(athlete, lb_session, [(lb_squat, 1, "1", "300", "9")])
        meso_one_rm.refresh_one_rms(athlete, [lb_squat], Unit.POUNDS)
        values = meso_one_rm.one_rm_values(athlete, [lb_squat], Unit.POUNDS)
        assert values[lb_squat.pk].value == Decimal("300.00")


class TestCleanManualValue:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_blank_means_clear(self, raw):
        value, ok = meso_one_rm.clean_manual_value(raw)
        assert ok is True
        assert value is None

    def test_a_number_is_quantized(self):
        value, ok = meso_one_rm.clean_manual_value("140")
        assert ok is True
        assert value == Decimal("140.00")

    @pytest.mark.parametrize(
        "raw", ["0", "-5", "abc", "heavy", "1e9", "nan", "inf", "-inf"]
    )
    def test_a_non_positive_non_numeric_or_non_finite_value_is_rejected(self, raw):
        # "nan"/"inf" parse to a float but must be rejected before _quantize,
        # which would raise on a non-finite Decimal (a 500, not the intended 400).
        value, ok = meso_one_rm.clean_manual_value(raw)
        assert ok is False
        assert value is None

    def test_an_overflowing_value_is_rejected(self):
        value, ok = meso_one_rm.clean_manual_value("1000000")
        assert ok is False
        assert value is None


class TestSetManualOneRm:
    def test_creates_a_manual_row(self):
        athlete = UserFactory()
        _, _, (squat,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        row = meso_one_rm.set_manual_one_rm(
            athlete, squat, Decimal("160"), Unit.KILOGRAMS
        )
        assert row.value == Decimal("160")
        assert row.source == AthleteOneRm.Source.MANUAL
        assert row.unit == Unit.KILOGRAMS
        assert row.key == "name:back squat"

    def test_overrides_an_existing_logged_row(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "150", "9")])
        meso_one_rm.refresh_one_rms(athlete, [squat], Unit.KILOGRAMS)
        meso_one_rm.set_manual_one_rm(athlete, squat, Decimal("200"), Unit.KILOGRAMS)
        rows = AthleteOneRm.objects.filter(athlete=athlete, key="name:back squat")
        assert rows.count() == 1
        row = rows.get()
        assert row.value == Decimal("200")
        assert row.source == AthleteOneRm.Source.MANUAL

    def test_clearing_reverts_to_the_log_derived_value(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "150", "9")])
        meso_one_rm.set_manual_one_rm(athlete, squat, Decimal("200"), Unit.KILOGRAMS)
        row = meso_one_rm.set_manual_one_rm(athlete, squat, None, Unit.KILOGRAMS)
        assert row is not None
        assert row.value == Decimal("150.00")
        assert row.source == AthleteOneRm.Source.LOGGED

    def test_clearing_with_no_logs_leaves_nothing(self):
        athlete = UserFactory()
        _, _, (squat,) = make_session(athlete, prescriptions=[{"name": "Back Squat"}])
        meso_one_rm.set_manual_one_rm(athlete, squat, Decimal("200"), Unit.KILOGRAMS)
        row = meso_one_rm.set_manual_one_rm(athlete, squat, None, Unit.KILOGRAMS)
        assert row is None
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()


class TestSetOneRmEndpoint:
    def test_sets_a_manual_estimate(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(athlete)
        resp = post_one_rm(client, session, {"prescription": squat.pk, "value": "160"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["one_rm"] == "160"
        assert body["source"] == "manual"
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("160.00")
        assert row.source == AthleteOneRm.Source.MANUAL

    def test_clears_a_manual_estimate(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(athlete)
        post_one_rm(client, session, {"prescription": squat.pk, "value": "160"})
        resp = post_one_rm(client, session, {"prescription": squat.pk, "value": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["one_rm"] == ""
        assert body["source"] == ""
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_manual_value_survives_a_later_log(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(athlete)
        post_one_rm(client, session, {"prescription": squat.pk, "value": "200"})
        post_log(
            client,
            session,
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": squat.pk,
                        "set_number": 1,
                        "reps": "1",
                        "load": "150",
                        "rpe": "9",
                    }
                ],
            },
        )
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("200.00")
        assert row.source == AthleteOneRm.Source.MANUAL

    @pytest.mark.parametrize("value", ["abc", "0", "-10", "nan"])
    def test_rejects_a_bad_value(self, client, value):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        client.force_login(athlete)
        resp = post_one_rm(client, session, {"prescription": squat.pk, "value": value})
        assert resp.status_code == 400
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_unknown_prescription_is_400(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        # A prescription that exists but in another session is not this session's.
        _, _, (other,) = make_session(athlete, prescriptions=[{"name": "Bench"}])
        client.force_login(athlete)
        resp = post_one_rm(client, session, {"prescription": other.pk, "value": "100"})
        assert resp.status_code == 400
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_foreign_session_is_404(self, client):
        owner = UserFactory()
        _, session, (squat,) = make_session(
            owner, prescriptions=[{"name": "Back Squat"}]
        )
        intruder = UserFactory()
        client.force_login(intruder)
        resp = post_one_rm(client, session, {"prescription": squat.pk, "value": "100"})
        assert resp.status_code == 404
        assert not AthleteOneRm.objects.filter(athlete=intruder).exists()

    def test_login_required(self, client):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        resp = post_one_rm(client, session, {"prescription": squat.pk, "value": "100"})
        assert resp.status_code in (301, 302)
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()


class TestPresenterCarriesSource:
    def test_athlete_session_carries_one_rm_source(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[
                {"name": "Back Squat", "load": "75", "load_type": LoadType.PERCENT}
            ],
        )
        AthleteOneRmFactory(
            athlete=athlete,
            name="Back Squat",
            value=Decimal("140"),
            source=AthleteOneRm.Source.MANUAL,
        )
        ctx = presenters.athlete_session(session, athlete)
        assert ctx["exercises"][0]["one_rm_source"] == "manual"
        payload = presenters.athlete_log_payload(ctx)
        assert payload["exercises"][0]["one_rm_source"] == "manual"
        assert payload["one_rm_url"] == reverse(
            "meso:athlete_set_one_rm", kwargs={"pk": session.pk}
        )

    def test_no_row_yields_empty_source(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        ctx = presenters.athlete_session(session, athlete)
        assert ctx["exercises"][0]["one_rm_source"] == ""


# -- coach-editable 1RM (1RM Phase 3) ---------------------------------------


def post_coach_one_rm(client, plan, prescription, payload):
    return client.post(
        reverse(
            "meso:api_coach_set_one_rm",
            kwargs={"plan_id": plan.pk, "pk": prescription.pk},
        ),
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestCoachSetOneRmEndpoint:
    """The coach sets/clears the athlete's 1RM from the designer's %1RM badge.

    The athlete logger already persists a manual 1RM (Phase 2); Phase 3 gives the
    coach the same write path, scoped to a plan they own, so a %1RM target they
    prescribe resolves to a real bar load even before the athlete logs the lift.
    """

    def test_coach_sets_an_athletes_manual_1rm(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete,
            coach=coach,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(coach)
        resp = post_coach_one_rm(client, plan, squat, {"value": "150"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["one_rm"] == "150"
        assert body["source"] == "manual"
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.value == Decimal("150.00")
        assert row.source == AthleteOneRm.Source.MANUAL
        assert row.unit == Unit.KILOGRAMS

    def test_coach_value_is_in_the_plans_unit(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete,
            coach=coach,
            unit=Unit.POUNDS,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        client.force_login(coach)
        post_coach_one_rm(client, plan, squat, {"value": "315"})
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.unit == Unit.POUNDS

    def test_coach_clears_back_to_log_derived(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        plan, session, (squat,) = make_session(
            athlete,
            coach=coach,
            prescriptions=[{"name": "Back Squat", "load_type": LoadType.PERCENT}],
        )
        # A logged set means clearing the manual value reverts to the estimate.
        log_session(athlete, session, [(squat, 1, "1", "120", "9")])
        client.force_login(coach)
        post_coach_one_rm(client, plan, squat, {"value": "200"})
        resp = post_coach_one_rm(client, plan, squat, {"value": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["one_rm"] == "120"
        assert body["source"] == "logged"
        row = AthleteOneRm.objects.get(athlete=athlete, key="name:back squat")
        assert row.source == AthleteOneRm.Source.LOGGED

    @pytest.mark.parametrize("value", ["abc", "0", "-5", "nan"])
    def test_rejects_a_bad_value(self, client, value):
        coach = UserFactory()
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete, coach=coach, prescriptions=[{"name": "Back Squat"}]
        )
        client.force_login(coach)
        resp = post_coach_one_rm(client, plan, squat, {"value": value})
        assert resp.status_code == 400
        assert not AthleteOneRm.objects.filter(athlete=athlete).exists()

    def test_group_plan_is_400(self, client):
        # A group plan has no single athlete, so a 1RM does not apply.
        coach = UserFactory()
        group_plan = GroupPlanFactory(status=Plan.Status.ACTIVE, group__coach=coach)
        meso = MesocycleFactory(plan=group_plan, order=0)
        week = WeekFactory(mesocycle=meso, index=1, is_current=True)
        session = SessionFactory(week=week, day_number=1)
        squat = ExercisePrescriptionFactory(
            session=session, order=0, name="Back Squat", load_type=LoadType.PERCENT
        )
        client.force_login(coach)
        resp = post_coach_one_rm(client, group_plan, squat, {"value": "150"})
        assert resp.status_code == 400
        assert not AthleteOneRm.objects.exists()

    def test_foreign_coach_is_403(self, client):
        plan, _, (squat,) = make_session(
            UserFactory(), prescriptions=[{"name": "Back Squat"}]
        )
        intruder = UserFactory()
        client.force_login(intruder)
        resp = post_coach_one_rm(client, plan, squat, {"value": "150"})
        assert resp.status_code == 403
        assert not AthleteOneRm.objects.exists()

    def test_prescription_outside_the_plan_is_404(self, client):
        coach = UserFactory()
        # The same coach training two athletes is over the free seat cap; comp
        # so this exercises the cross-plan 404, not the D6 over-limit 402.
        CoachSubscription.comp(coach)
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete, coach=coach, prescriptions=[{"name": "Back Squat"}]
        )
        # A prescription that exists but in another plan (a different athlete the
        # same coach trains) is not this plan's.
        _, _, (other,) = make_session(
            UserFactory(), coach=coach, prescriptions=[{"name": "Bench"}]
        )
        client.force_login(coach)
        resp = post_coach_one_rm(client, plan, other, {"value": "150"})
        assert resp.status_code == 404

    def test_login_required(self, client):
        plan, _, (squat,) = make_session(
            UserFactory(), prescriptions=[{"name": "Back Squat"}]
        )
        resp = post_coach_one_rm(client, plan, squat, {"value": "150"})
        assert resp.status_code in (301, 302)
        assert not AthleteOneRm.objects.exists()


class TestSerializerCarriesSource:
    def test_individual_plan_row_carries_one_rm_source(self):
        athlete = UserFactory()
        plan, _, (squat,) = make_session(
            athlete,
            prescriptions=[
                {"name": "Back Squat", "load": "75", "load_type": LoadType.PERCENT}
            ],
        )
        AthleteOneRmFactory(
            athlete=athlete,
            name="Back Squat",
            value=Decimal("140"),
            source=AthleteOneRm.Source.MANUAL,
        )
        data = serialize_plan(plan)
        row = data["program"][0]["exercises"][0]
        assert row["one_rm"] == "140"
        assert row["one_rm_source"] == "manual"
