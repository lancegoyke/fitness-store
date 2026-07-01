"""Groups slice (S1) Phase 2a — the shared group program.

Phase 1 stood up the group + membership spine and a read surface. Phase 2a
gives a group a **shared program**: a ``Plan`` rooted at a ``MesoGroup`` instead
of a ``CoachAthlete`` relationship. A plan is now *either* individual
(``relationship`` set) *or* group (``group`` set) — never both, never neither
(a DB ``XOR`` constraint). The designer renders a group plan in Group mode and
the coach can edit its grid; per-athlete auto-adjusts, the group agent, and
deliver-to-all are later phases (3/4), so the agent + deliver endpoints reject
group plans for now.

These tests pin: the model shape (constraint, ``coach``/``athlete``/``is_group``),
the editable-by scoping (the designer + autosave surface accepts a group plan
the coach owns; ``for_coach`` stays individual-only so the individual-only
deliver/results/review flows never see a group plan), the shared-plan helpers,
the serialized group payload the designer hydrates Group mode from, and the
group-design entry point. See ``docs/archive/meso/groups-plan.md``.
"""

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse

from store_project.meso import serializers
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import Week
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_member(group, *, name="Member One"):
    """An athlete with an active link to the group's coach, added to the group."""
    athlete = UserFactory(name=name)
    CoachAthleteFactory(
        coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    return group.add_athlete(athlete)


# -- model: the relationship XOR group constraint ---------------------------


class TestPlanRootConstraint:
    def test_group_plan_has_no_relationship(self):
        plan = GroupPlanFactory()
        assert plan.relationship_id is None
        assert plan.group_id is not None
        assert plan.is_group is True

    def test_individual_plan_is_not_group(self):
        plan = PlanFactory()
        assert plan.group_id is None
        assert plan.relationship_id is not None
        assert plan.is_group is False

    def test_plan_with_both_roots_is_rejected(self):
        group = MesoGroupFactory()
        link = CoachAthleteFactory(coach=group.coach)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Plan.objects.create(relationship=link, group=group, title="Both roots")

    def test_plan_with_no_root_is_rejected(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Plan.objects.create(relationship=None, group=None, title="Orphan")


# -- model: coach / athlete / str -------------------------------------------


class TestGroupPlanAccessors:
    def test_coach_is_the_groups_coach(self):
        group = MesoGroupFactory()
        plan = GroupPlanFactory(group=group)
        assert plan.coach == group.coach

    def test_athlete_is_none_for_a_group_plan(self):
        plan = GroupPlanFactory()
        assert plan.athlete is None

    def test_str_names_the_group(self):
        group = MesoGroupFactory(name="Tue/Thu Squad")
        plan = GroupPlanFactory(group=group, title="Strength")
        assert "Tue/Thu Squad" in str(plan)


# -- model: scoping ---------------------------------------------------------


class TestPlanScoping:
    def test_editable_by_includes_owned_group_plan(self):
        group = MesoGroupFactory()
        plan = GroupPlanFactory(group=group)
        assert plan in Plan.objects.editable_by(group.coach)

    def test_editable_by_includes_owned_individual_active_plan(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.ACTIVE)
        plan = PlanFactory(relationship=link)
        assert plan in Plan.objects.editable_by(link.coach)

    def test_editable_by_excludes_foreign_group_plan(self):
        plan = GroupPlanFactory()
        stranger = UserFactory()
        assert plan not in Plan.objects.editable_by(stranger)

    def test_editable_by_excludes_inactive_individual_plan(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.ACTIVE)
        plan = PlanFactory(relationship=link)
        link.end()
        assert plan not in Plan.objects.editable_by(link.coach)

    def test_for_coach_stays_individual_only(self):
        # Group plans must NOT leak into ``for_coach`` — the individual-only
        # deliver/results/review flows scope by it and assume an athlete.
        group = MesoGroupFactory()
        group_plan = GroupPlanFactory(group=group)
        assert group_plan not in Plan.objects.for_coach(group.coach)

    def test_for_athlete_excludes_group_plans(self):
        member = make_member(MesoGroupFactory(), name="Aaron Adams")
        athlete = member.relationship.athlete
        GroupPlanFactory(group=member.group)
        assert list(Plan.objects.for_athlete(athlete)) == []

    def test_is_editable_by(self):
        group = MesoGroupFactory()
        plan = GroupPlanFactory(group=group)
        assert plan.is_editable_by(group.coach) is True
        assert plan.is_editable_by(UserFactory()) is False


# -- model: shared-plan helpers ---------------------------------------------


class TestSharedPlanHelpers:
    def test_shared_plan_none_when_absent(self):
        assert MesoGroupFactory().shared_plan() is None

    def test_create_shared_plan_roots_at_group_with_scaffold(self):
        group = MesoGroupFactory(name="Squad", focus="Strength")
        plan = group.create_shared_plan()
        assert plan.group_id == group.pk
        assert plan.relationship_id is None
        # A usable starter scaffold so the designer is not blank (there is no
        # add-session/add-week endpoint yet — an empty plan would be uneditable).
        assert Mesocycle.objects.filter(plan=plan).exists()
        assert Week.objects.filter(mesocycle__plan=plan).exists()
        assert Session.objects.filter(week__mesocycle__plan=plan).exists()

    def test_shared_plan_returns_created_plan(self):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        assert group.shared_plan() == plan

    def test_shared_plan_excludes_archived(self):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        plan.status = Plan.Status.ARCHIVED
        plan.save(update_fields=["status"])
        assert group.shared_plan() is None


# -- serializer: the group payload the designer hydrates from ----------------


class TestSerializeGroupPlan:
    def test_group_plan_serializes_a_group_payload(self):
        group = MesoGroupFactory(name="Squad", focus="Strength")
        m1 = make_member(group, name="Aaron Adams")
        make_member(group, name="Beth Brown")
        ContraindicationFactory(
            athlete=m1.relationship.athlete, text="L knee — avoid deep flexion"
        )
        plan = group.create_shared_plan()
        data = serializers.serialize_plan(plan)
        assert data["group"]["name"] == "Squad"
        assert data["group"]["focus"] == "Strength"
        assert [m["name"] for m in data["group"]["members"]] == [
            "Aaron Adams",
            "Beth Brown",
        ]
        assert data["group"]["members"][0]["initials"] == "AA"
        assert data["group"]["flags"] == ["L knee"]

    def test_individual_plan_has_no_group_payload(self):
        data = serializers.serialize_plan(PlanFactory())
        assert data["group"] is None

    def test_group_plan_serialization_omits_last_column(self):
        # A group plan has no single athlete, so the "last time" column (which is
        # athlete-scoped) must be skipped — and never crash on ``plan.athlete``.
        group = MesoGroupFactory()
        make_member(group, name="Aaron Adams")
        plan = group.create_shared_plan()
        data = serializers.serialize_plan(plan)
        for session in data["program"]:
            for exercise in session["exercises"]:
                assert "last" not in exercise


# -- view: the group-design entry point -------------------------------------


class TestGroupDesign:
    def test_post_creates_shared_plan_and_redirects_to_designer(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        group = MesoGroupFactory(coach=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:group_design", kwargs={"pk": group.pk}))
        plan = group.shared_plan()
        assert plan is not None
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})

    def test_post_is_idempotent(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        group = MesoGroupFactory(coach=coach)
        client.force_login(coach)
        url = reverse("meso:group_design", kwargs={"pk": group.pk})
        client.post(url)
        client.post(url)
        assert group.plans.count() == 1

    def test_foreign_group_is_404(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=UserFactory())
        client.force_login(coach)
        resp = client.post(reverse("meso:group_design", kwargs={"pk": group.pk}))
        assert resp.status_code == 404

    def test_get_not_allowed(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:group_design", kwargs={"pk": group.pk}))
        assert resp.status_code == 405

    def test_requires_login(self, client):
        group = MesoGroupFactory()
        resp = client.post(reverse("meso:group_design", kwargs={"pk": group.pk}))
        assert resp.status_code == 302


# -- view: the designer renders a group plan --------------------------------


class TestDesignerGroupPlan:
    def test_coach_opens_group_plan(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach, name="Squad")
        make_member(group, name="Aaron Adams")
        plan = group.create_shared_plan()
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        # The serialized group identity rides the JSON island the JS hydrates.
        assert "Squad" in body
        assert '"group"' in body

    def test_foreign_coach_group_plan_is_404(self, client):
        group = MesoGroupFactory(coach=UserFactory())
        plan = group.create_shared_plan()
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404

    def test_bare_designer_redirects_to_a_group_working_plan(self, client):
        # The bare /designer/ target spans both kinds: a coach whose most-recent
        # plan is a group's shared program lands back on it, not an older one.
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        PlanFactory(relationship=link)  # an older individual plan
        group = MesoGroupFactory(coach=coach)
        group_plan = group.create_shared_plan()  # the most-recently created plan
        client.force_login(coach)
        resp = client.get(reverse("meso:designer"))
        assert resp.status_code == 302
        assert resp.url == reverse(
            "meso:designer_plan", kwargs={"plan_id": group_plan.pk}
        )


# -- view: autosave works on a group plan, athlete-only endpoints don't -----


class TestGroupPlanEndpoints:
    def _session(self, plan):
        return Session.objects.filter(week__mesocycle__plan=plan).first()

    def test_add_exercise_works_for_group_coach(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        plan = group.create_shared_plan()
        session = self._session(plan)
        client.force_login(coach)
        resp = client.post(
            reverse(
                "meso:api_session_add_exercise",
                kwargs={"plan_id": plan.pk, "pk": session.pk},
            )
        )
        assert resp.status_code == 201

    def test_add_exercise_forbidden_for_foreign_coach(self, client):
        group = MesoGroupFactory(coach=UserFactory())
        plan = group.create_shared_plan()
        session = self._session(plan)
        client.force_login(UserFactory())
        resp = client.post(
            reverse(
                "meso:api_session_add_exercise",
                kwargs={"plan_id": plan.pk, "pk": session.pk},
            )
        )
        assert resp.status_code == 403

    def test_deliver_group_without_members_400(self, client):
        # Deliver-to-all (Phase 4) fans out to active members; a group with none
        # is a 400 (delivering to nobody is a coach mistake, not a silent no-op).
        # The full fan-out is covered in ``test_group_deliver.py``.
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        plan = group.create_shared_plan()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
        assert resp.status_code == 400

    def test_agent_accepts_group_plan(self, client, monkeypatch):
        # The group agent (Phase 1) edits the SHARED program: a group plan is no
        # longer a 400 — it grounds on the group and runs behind the review gate.
        # Full coverage lives in ``test_group_agent.py``.
        from store_project.meso.agent import client as agent_client_module
        from store_project.meso.tests.test_agent_service import FakeClient

        coach = UserFactory()
        # The AI agent is paid-only (S6 Phase 3, D4), so a coach iterating a plan
        # with the agent in these tests has full access — comp keeps the gate open.
        CoachSubscription.comp(coach)
        group = MesoGroupFactory(coach=coach)
        plan = group.create_shared_plan()
        monkeypatch.setattr(
            agent_client_module,
            "get_default_client",
            lambda: FakeClient({"summary": "", "changes": []}),
        )
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data={"instruction": "progress loads"},
            content_type="application/json",
        )
        assert resp.status_code == 202


# -- view: group detail surfaces the shared-program entry point --------------


class TestGroupDetailSharedProgram:
    def test_shows_design_button_when_no_plan(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        client.force_login(coach)
        body = client.get(
            reverse("meso:group", kwargs={"pk": group.pk})
        ).content.decode()
        assert reverse("meso:group_design", kwargs={"pk": group.pk}) in body

    def test_shows_open_link_when_plan_exists(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        plan = group.create_shared_plan()
        client.force_login(coach)
        body = client.get(
            reverse("meso:group", kwargs={"pk": group.pk})
        ).content.decode()
        assert reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}) in body
