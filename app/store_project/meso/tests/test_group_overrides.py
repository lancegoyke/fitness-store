"""Groups slice (S1) Phase 3 — per-athlete overrides (the ``adj`` overlay).

Phase 2a gave a group a **shared program** (a ``Plan`` rooted at a
``MesoGroup``) every member trains off. Phase 3 layers each member's
**auto-adjusts** on top: a ``PrescriptionOverride`` per (member, shared
prescription) carrying a swap, a load %, or a volume tweak, so a member's
*effective* program = the shared template **+** that member's override diffs.
The designer's shared grid surfaces these as a per-row ``adj`` badge driven by
the real diffs (no more fabricated adjusts).

These tests pin: the model shape (unique per member+prescription, the
same-group tenancy guard), the ``set_override`` / ``clear_override`` helpers,
the effective-program resolution + the ``adj`` label, the serialized ``adj``
overlay the designer renders, and the override API endpoint. See
``docs/meso/groups-plan.md``.
"""

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse

from store_project.meso import serializers
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import PrescriptionOverrideFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import ExercisePrescription
from store_project.meso.models import InvalidTransition
from store_project.meso.models import PrescriptionOverride
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_member(group, *, name="Member One"):
    """An athlete with an active link to the group's coach, added to the group."""
    athlete = UserFactory(name=name)
    CoachAthleteFactory(
        coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    return group.add_athlete(athlete)


def first_prescription(plan):
    """The first prescription on a (shared) plan's scaffold."""
    return ExercisePrescription.objects.filter(
        session__week__mesocycle__plan=plan
    ).first()


# -- model: shape + tenancy -------------------------------------------------


class TestPrescriptionOverrideModel:
    def test_one_override_per_member_and_prescription(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        PrescriptionOverrideFactory(
            membership=membership, prescription=presc, load_pct=90
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PrescriptionOverrideFactory(
                    membership=membership, prescription=presc, load_pct=80
                )

    def test_has_diff_true_when_any_field_set(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        override = PrescriptionOverrideFactory(
            membership=membership, prescription=presc, swap_name="Box Squat"
        )
        assert override.has_diff is True

    def test_has_diff_false_when_empty(self):
        override = PrescriptionOverride()
        assert override.has_diff is False

    def test_clean_rejects_cross_group_prescription(self):
        from django.core.exceptions import ValidationError

        group_a = MesoGroupFactory()
        membership_a = make_member(group_a)
        other_plan = PlanFactory(group=MesoGroupFactory(), relationship=None)
        foreign_presc = ExercisePrescriptionFactory(
            session__week__mesocycle__plan=other_plan
        )
        override = PrescriptionOverride(
            membership=membership_a, prescription=foreign_presc, load_pct=90
        )
        with pytest.raises(ValidationError):
            override.full_clean()

    def test_str_names_the_athlete(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Maya Okonkwo")
        plan = group.create_shared_plan()
        override = PrescriptionOverrideFactory(
            membership=membership, prescription=first_prescription(plan), load_pct=90
        )
        assert "Maya Okonkwo" in str(override)


# -- helpers: set_override / clear_override ----------------------------------


class TestSetOverride:
    def test_set_override_creates(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        override = membership.set_override(presc, load_pct=90, swap_name="Box Squat")
        assert override is not None
        assert override.load_pct == 90
        assert override.swap_name == "Box Squat"
        assert membership.overrides.count() == 1

    def test_set_override_updates_existing(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        membership.set_override(presc, load_pct=80)
        assert membership.overrides.count() == 1
        assert membership.overrides.first().load_pct == 80

    def test_set_override_normalizes_noop_load_pct(self):
        # load_pct=100 is a no-op (100% of the shared load) — not stored as a diff.
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        assert membership.set_override(presc, load_pct=100) is None
        assert membership.overrides.count() == 0

    def test_empty_diff_returns_none_and_clears(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        assert membership.set_override(presc) is None  # no diff → cleared
        assert membership.overrides.count() == 0

    def test_set_override_rejects_cross_group_prescription(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        other_plan = PlanFactory(group=MesoGroupFactory(), relationship=None)
        foreign_presc = ExercisePrescriptionFactory(
            session__week__mesocycle__plan=other_plan
        )
        with pytest.raises(InvalidTransition):
            membership.set_override(foreign_presc, load_pct=90)

    def test_clear_override_deletes(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        membership.clear_override(presc)
        assert membership.overrides.count() == 0

    def test_clear_override_is_a_noop_when_absent(self):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.clear_override(presc)  # no exception
        assert membership.overrides.count() == 0


# -- resolution: effective program (shared + override) ----------------------


class TestResolvePrescription:
    def test_none_override_yields_base(self):
        presc = ExercisePrescriptionFactory(
            name="Back Squat", sets="3", reps="10", load="100", rpe="7"
        )
        resolved = serializers.resolve_prescription(presc, None)
        assert resolved["name"] == "Back Squat"
        assert resolved["load"] == "100"

    def test_swap_replaces_name(self):
        presc = ExercisePrescriptionFactory(name="Back Squat")
        override = PrescriptionOverride(swap_name="Box Squat")
        assert serializers.resolve_prescription(presc, override)["name"] == "Box Squat"

    def test_load_pct_scales_numeric_load_rounded_to_2_5(self):
        presc = ExercisePrescriptionFactory(load="100")
        override = PrescriptionOverride(load_pct=90)
        assert serializers.resolve_prescription(presc, override)["load"] == "90"

    def test_load_pct_rounds_half_up_like_the_designer(self):
        # 112.5 @ 90% = 101.25 → 40.5 half-steps. The designer's Math.round rounds
        # the half up to 102.5; Python's banker's round would give 100, so the
        # resolver must match the UI here.
        presc = ExercisePrescriptionFactory(load="112.5")
        override = PrescriptionOverride(load_pct=90)
        assert serializers.resolve_prescription(presc, override)["load"] == "102.5"

    def test_load_pct_leaves_non_numeric_load_unchanged(self):
        presc = ExercisePrescriptionFactory(load="BW")
        override = PrescriptionOverride(load_pct=90)
        assert serializers.resolve_prescription(presc, override)["load"] == "BW"

    def test_volume_overrides_sets_and_reps(self):
        presc = ExercisePrescriptionFactory(sets="3", reps="10")
        override = PrescriptionOverride(sets="2", reps="8")
        resolved = serializers.resolve_prescription(presc, override)
        assert resolved["sets"] == "2"
        assert resolved["reps"] == "8"


class TestOverrideAdjLabel:
    def test_swap_label(self):
        override = PrescriptionOverride(swap_name="Box Squat")
        assert serializers.override_adj_label(override) == "→ Box Squat"

    def test_load_decrease_label(self):
        override = PrescriptionOverride(load_pct=90)
        assert serializers.override_adj_label(override) == "-10%"

    def test_load_increase_label(self):
        override = PrescriptionOverride(load_pct=105)
        assert serializers.override_adj_label(override) == "+5%"

    def test_volume_label(self):
        override = PrescriptionOverride(sets="2", reps="8")
        assert serializers.override_adj_label(override) == "2×8"

    def test_combined_label(self):
        override = PrescriptionOverride(swap_name="Box Squat", load_pct=90)
        assert serializers.override_adj_label(override) == "→ Box Squat · -10%"


# -- aggregation: group_adjustments -----------------------------------------


class TestGroupAdjustments:
    def test_single_override_shows_initials_and_label(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Maya Okonkwo")
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        adj_map = serializers.group_adjustments(plan, [presc])
        assert adj_map[presc.pk]["adj"] == "MO -10%"
        assert adj_map[presc.pk]["adjusts"][0]["initials"] == "MO"
        assert adj_map[presc.pk]["adjusts"][0]["label"] == "-10%"

    def test_multiple_overrides_collapse_to_count(self):
        group = MesoGroupFactory()
        m1 = make_member(group, name="Aaron Adams")
        m2 = make_member(group, name="Beth Brown")
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        m1.set_override(presc, load_pct=90)
        m2.set_override(presc, swap_name="Box Squat")
        adj_map = serializers.group_adjustments(plan, [presc])
        assert adj_map[presc.pk]["adj"] == "2 adjusts"
        assert len(adj_map[presc.pk]["adjusts"]) == 2

    def test_excludes_ended_member_override(self):
        group = MesoGroupFactory()
        m1 = make_member(group, name="Aaron Adams")
        m2 = make_member(group, name="Beth Brown")
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        m1.set_override(presc, load_pct=90)
        m2.set_override(presc, load_pct=80)
        m2.relationship.end()  # Beth leaves — her adjust drops off the badge
        adj_map = serializers.group_adjustments(plan, [presc])
        assert adj_map[presc.pk]["adj"] == "AA -10%"

    def test_empty_when_no_overrides(self):
        group = MesoGroupFactory()
        make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        assert serializers.group_adjustments(plan, [presc]) == {}


# -- serializer: the adj overlay the designer renders -----------------------


class TestSerializeAdjOverlay:
    def test_group_plan_emits_adj_on_overridden_rows(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Maya Okonkwo")
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        data = serializers.serialize_plan(plan)
        row = next(
            ex
            for session in data["program"]
            for ex in session["exercises"]
            if ex["id"] == presc.pk
        )
        assert row["adj"] == "MO -10%"
        assert row["adjusts"][0]["label"] == "-10%"

    def test_group_plan_row_without_override_has_no_adj(self):
        group = MesoGroupFactory()
        make_member(group)
        plan = group.create_shared_plan()
        data = serializers.serialize_plan(plan)
        for session in data["program"]:
            for ex in session["exercises"]:
                assert "adj" not in ex

    def test_individual_plan_never_emits_adj(self):
        plan = PlanFactory()
        ExercisePrescriptionFactory(
            session__week__mesocycle__plan=plan,
        )
        # individual plan: even with a session it carries no adj overlay
        data = serializers.serialize_plan(plan)
        for session in data["program"]:
            for ex in session["exercises"]:
                assert "adj" not in ex

    def test_group_identity_members_carry_athlete_id(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Maya Okonkwo")
        plan = group.create_shared_plan()
        data = serializers.serialize_plan(plan)
        assert data["group"]["members"][0]["id"] == str(
            membership.relationship.athlete.pk
        )


# -- view: the override API endpoint ----------------------------------------


class TestOverrideEndpoint:
    def _url(self, plan, presc):
        return reverse(
            "meso:api_prescription_override",
            kwargs={"plan_id": plan.pk, "pk": presc.pk},
        )

    def test_coach_sets_override(self, client):
        group = MesoGroupFactory()
        membership = make_member(group, name="Maya Okonkwo")
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        client.force_login(group.coach)
        resp = client.post(
            self._url(plan, presc),
            data={"athlete": str(membership.relationship.athlete.pk), "load_pct": 90},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert membership.overrides.count() == 1
        assert resp.json()["adj"] == "MO -10%"

    def test_coach_clears_override(self, client):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        membership.set_override(presc, load_pct=90)
        client.force_login(group.coach)
        resp = client.post(
            self._url(plan, presc),
            data={
                "athlete": str(membership.relationship.athlete.pk),
                "clear": True,
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert membership.overrides.count() == 0

    def test_foreign_coach_forbidden(self, client):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        client.force_login(UserFactory())
        resp = client.post(
            self._url(plan, presc),
            data={"athlete": str(membership.relationship.athlete.pk), "load_pct": 90},
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_individual_plan_rejected(self, client):
        plan = PlanFactory()
        presc = ExercisePrescriptionFactory(session__week__mesocycle__plan=plan)
        client.force_login(plan.coach)
        resp = client.post(
            self._url(plan, presc),
            data={"athlete": str(plan.athlete.pk), "load_pct": 90},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_member_athlete_rejected(self, client):
        group = MesoGroupFactory()
        make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        stranger = UserFactory()
        client.force_login(group.coach)
        resp = client.post(
            self._url(plan, presc),
            data={"athlete": str(stranger.pk), "load_pct": 90},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_bad_load_pct_rejected(self, client):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        client.force_login(group.coach)
        resp = client.post(
            self._url(plan, presc),
            data={
                "athlete": str(membership.relationship.athlete.pk),
                "load_pct": 9000,
            },
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_foreign_prescription_is_404(self, client):
        group = MesoGroupFactory()
        membership = make_member(group)
        plan = group.create_shared_plan()
        other_plan = group.create_shared_plan()  # a second plan on the same group
        foreign_presc = first_prescription(other_plan)
        client.force_login(group.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_override",
                kwargs={"plan_id": plan.pk, "pk": foreign_presc.pk},
            ),
            data={"athlete": str(membership.relationship.athlete.pk), "load_pct": 90},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_get_not_allowed(self, client):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        client.force_login(group.coach)
        resp = client.get(self._url(plan, presc))
        assert resp.status_code == 405

    def test_requires_login(self, client):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        presc = first_prescription(plan)
        resp = client.post(self._url(plan, presc))
        assert resp.status_code == 302
