"""Units & RPE/%1RM slice (S2) Phase 1 — first-class %1RM load typing.

Units (kg/lb) shipped with earlier slices (``Unit`` / ``Plan.unit``); the gap
S2 closes is **how intensity is prescribed**: today the designer's Load number
always means an absolute load in the plan's unit, with no way to say "75% of
1RM". This phase adds a ``load_type`` (``ABSOLUTE`` / ``PERCENT``) on the
prescription — the load number's *meaning* — carried through the serializer, the
per-athlete override resolver, and the group deliver fan-out, and rendered as a
``%`` suffix (vs the unit) on the coach designer + the athlete surfaces.

RPE is unchanged and orthogonal (its own column, coexists with either type).
A ``LoggedSet`` has no ``load_type`` — an athlete always logs the actual weight
lifted (absolute). See ``docs/archive/meso/units-rpe-plan.md``.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoadType
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import PrescriptionOverride
from store_project.meso.models import Unit
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as make_presc

pytestmark = pytest.mark.django_db


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    presc = make_presc(
        session, name="Back Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return plan, session, presc


# -- model -------------------------------------------------------------------


class TestLoadTypeModel:
    def test_defaults_to_absolute(self):
        _, _, presc = seed_plan()
        presc.refresh_from_db()
        assert presc.load_type == LoadType.ABSOLUTE

    def test_accepts_percent(self):
        _, _, presc = seed_plan()
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load_type"])
        presc.refresh_from_db()
        assert presc.load_type == LoadType.PERCENT


# -- serializer + resolver ---------------------------------------------------


class TestSerializeLoadType:
    def test_serialize_prescription_emits_load_type(self):
        _, _, presc = seed_plan()
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load_type"])
        data = serializers.serialize_prescription(presc)
        assert data["load_type"] == LoadType.PERCENT

    def test_resolve_prescription_carries_type_with_no_override(self):
        _, _, presc = seed_plan()
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load_type"])
        resolved = serializers.resolve_prescription(presc, None)
        assert resolved["load_type"] == LoadType.PERCENT
        assert resolved["load"] == "70"

    def test_resolve_prescription_scales_percent_keeping_type(self):
        # A member at 90% of a 75%-1RM lift → 67.5% (scaled), still a percent.
        _, _, presc = seed_plan()
        presc.load = "75"
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load", "load_type"])
        override = PrescriptionOverride(load_pct=90)
        resolved = serializers.resolve_prescription(presc, override)
        assert resolved["load_type"] == LoadType.PERCENT
        assert resolved["load"] == "67.5"

    def test_percent_scaling_uses_a_half_percent_step_not_plates(self):
        # A %1RM isn't plate-constrained: 80% @ 90% is 72%, not the 72.5% that
        # absolute (2.5 plate) rounding would give.
        _, _, presc = seed_plan()
        presc.load = "80"
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load", "load_type"])
        override = PrescriptionOverride(load_pct=90)
        assert serializers.resolve_prescription(presc, override)["load"] == "72"

    def test_absolute_scaling_still_rounds_to_plates(self):
        # The absolute path is unchanged: 80 kg @ 90% → 72.5 (nearest 2.5 plate).
        _, _, presc = seed_plan()
        presc.load = "80"  # default ABSOLUTE
        presc.save(update_fields=["load"])
        override = PrescriptionOverride(load_pct=90)
        assert serializers.resolve_prescription(presc, override)["load"] == "72.5"


# -- autosave endpoint -------------------------------------------------------


class TestPrescriptionPatchLoadType:
    def _url(self, plan, presc):
        return reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": plan.pk, "pk": presc.pk},
        )

    def test_patch_writes_valid_load_type(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load_type": "pct"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        presc.refresh_from_db()
        assert presc.load_type == LoadType.PERCENT
        assert resp.json()["prescription"]["load_type"] == LoadType.PERCENT

    def test_patch_rejects_invalid_load_type(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load_type": "bogus"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        presc.refresh_from_db()
        assert presc.load_type == LoadType.ABSOLUTE  # nothing persisted

    def test_patch_load_type_non_owner_forbidden(self, client):
        plan, _, presc = seed_plan()
        client.force_login(UserFactory())  # not this plan's coach
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load_type": "pct"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        presc.refresh_from_db()
        assert presc.load_type == LoadType.ABSOLUTE


# -- presenter target labels -------------------------------------------------


class TestTargetLabels:
    def test_athlete_target_label_shows_percent(self):
        _, _, presc = seed_plan()
        presc.load = "75"
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load", "load_type"])
        label = presenters._target_label(presc)
        assert "75%" in label

    def test_athlete_target_label_absolute_has_no_percent(self):
        _, _, presc = seed_plan()  # default ABSOLUTE, load "70"
        label = presenters._target_label(presc)
        assert "70" in label
        assert "%" not in label

    def test_results_target_label_shows_percent(self):
        _, _, presc = seed_plan()
        presc.load = "80"
        presc.load_type = LoadType.PERCENT
        presc.save(update_fields=["load", "load_type"])
        label = presenters._results_target_label(presc, Unit.KILOGRAMS)
        assert "80%" in label
        assert "kg" not in label

    def test_results_target_label_absolute_shows_unit(self):
        _, _, presc = seed_plan()  # ABSOLUTE, load "70"
        label = presenters._results_target_label(presc, Unit.KILOGRAMS)
        assert "70 kg" in label


# -- group deliver fan-out ---------------------------------------------------


class TestGroupFanOutLoadType:
    def _seed_group(self):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        membership = group.add_athlete(athlete)
        plan = group.create_shared_plan()
        return group, plan, membership

    def test_materialized_member_prescription_preserves_load_type(self):
        group, plan, membership = self._seed_group()
        shared = Prescription.objects.filter(week__mesocycle__plan=plan).first()
        shared.load = "80"
        shared.load_type = LoadType.PERCENT
        shared.save(update_fields=["load", "load_type"])

        group.deliver_block(plan)

        member_plan = Plan.objects.get(
            relationship=membership.relationship, source_group=group
        )
        member_presc = Prescription.objects.filter(
            week__mesocycle__plan=member_plan,
            exercise_slot__order=shared.exercise_slot.order,
        ).first()
        assert member_presc is not None
        assert member_presc.load_type == LoadType.PERCENT
