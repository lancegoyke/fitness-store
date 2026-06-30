"""Group agent — Phase 2: the agent proposes per-athlete AUTO-ADJUSTS.

Phase 1 let the group agent edit the group's *shared* program (every member
inherits). Phase 2 adds the missing half: the agent can also propose a per-member
``PrescriptionOverride`` — a swap, a load %, or a volume tweak that diverges *one*
member from the shared base (the designer's ``adj`` overlay), without forking the
program. It rides the same propose → review → apply gate.

Two properties matter and are the point of this slice:

- **Targeting.** An ``adjust`` change names the member by ``member_id`` (the
  ``GroupMembership`` pk the grounding context exposes) and the shared
  prescription it overrides. It is group-only — an ``adjust`` on an individual
  plan is rejected.
- **Per-member safety.** Unlike a *shared* swap (screened against the folded set
  of every member's contraindications), an ``adjust`` swap only trains the one
  member, so it is screened against **that member's own** contraindications — a
  movement unsafe for a *different* member is allowed (it never reaches them).

Apply materializes the override via ``GroupMembership.set_override`` so it shows on
the designer overlay and flows through deliver-to-all (``sync_delivered_plan``)
exactly like a coach-authored adjust.
"""

import pytest
from django.urls import reverse

from store_project.meso.agent import apply as agent_apply
from store_project.meso.agent import client as agent_client
from store_project.meso.agent import service
from store_project.meso.agent import validation
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import GroupMembership
from store_project.meso.models import PrescriptionOverride
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_endpoint import install_fake
from store_project.meso.tests.test_agent_endpoint import propose
from store_project.meso.tests.test_agent_endpoint import status_url
from store_project.meso.tests.test_group_agent import first_presc
from store_project.meso.tests.test_group_agent import make_group_plan

pytestmark = pytest.mark.django_db


def membership_for(group, athlete):
    """The active ``GroupMembership`` linking ``athlete`` to ``group``."""
    return GroupMembership.objects.get(group=group, relationship__athlete=athlete)


def adjust_raw(*, member_id, presc, **overrides):
    """An ``adjust`` change dict in the shape the model returns through the tool."""
    raw = {
        "kind": "adjust",
        "member_id": member_id,
        "prescription_id": presc.pk,
        "day_label": "Day 1",
        "title": "Back off this member's squat",
        "before": "Back Squat",
        "after": "Back Squat −10%",
        "rationale": "This member is managing knee load.",
        "honors": "L knee",
        "new_name": "",
        "load_pct": 90,
        "new_sets": "",
        "new_reps": "",
    }
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------
# Grounding — the agent needs a stable per-member id to target an adjust.
# --------------------------------------------------------------------------


class TestGroupContextMemberIds:
    def test_each_member_carries_its_membership_id(self):
        coach, group, plan, athletes = make_group_plan()
        g = service.build_context(plan)["group"]
        ids = {m["member_id"] for m in g["members"]}
        expected = {membership_for(group, a).pk for a in athletes}
        assert ids == expected
        # And the per-member contraindication block is still there (Phase 1).
        assert all("contraindications" in m for m in g["members"])


# --------------------------------------------------------------------------
# Validation — an adjust resolves a member + a shared row and builds the diff.
# --------------------------------------------------------------------------


class TestAdjustValidation:
    def test_builds_the_override_payload(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        raw = adjust_raw(
            member_id=m.pk, presc=presc, new_name="Box Squat", load_pct=90, new_sets="4"
        )

        cleaned, errors = validation.clean_change(raw, plan)

        assert errors == []
        assert cleaned["kind"] == "adjust"
        assert cleaned["membership"] == m
        assert cleaned["prescription"] == presc
        assert cleaned["payload"]["swap_name"] == "Box Squat"
        assert cleaned["payload"]["load_pct"] == 90
        assert cleaned["payload"]["sets"] == "4"

    def test_requires_a_real_diff(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        # No swap, no volume, and a no-op 100% load — nothing to apply.
        raw = adjust_raw(
            member_id=m.pk, presc=presc, new_name="", load_pct=100, new_sets=""
        )

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("at least one" in e for e in errors)

    def test_drops_a_no_op_full_load(self):
        # load_pct=100 is 100% of shared = no change; it must not survive as a diff.
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        raw = adjust_raw(
            member_id=m.pk, presc=presc, new_name="Box Squat", load_pct=100
        )

        cleaned, errors = validation.clean_change(raw, plan)

        assert errors == []
        assert "load_pct" not in cleaned["payload"]
        assert cleaned["payload"]["swap_name"] == "Box Squat"

    def test_rejects_an_out_of_range_load_pct(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        raw = adjust_raw(member_id=m.pk, presc=presc, new_name="", load_pct=500)

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("load" in e.lower() for e in errors)

    def test_requires_a_member(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        raw = adjust_raw(member_id=None, presc=presc)

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("member" in e.lower() for e in errors)

    def test_rejects_an_unknown_member(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        raw = adjust_raw(member_id=999_999, presc=presc)

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("member" in e.lower() for e in errors)

    def test_rejects_a_member_of_another_group(self):
        coach, group, plan, athletes = make_group_plan()
        # A membership in a DIFFERENT group must not be targetable on this plan.
        _, other_group, _, other_athletes = make_group_plan(coach=coach)
        foreign = membership_for(other_group, other_athletes[0])
        presc = first_presc(plan)
        raw = adjust_raw(member_id=foreign.pk, presc=presc)

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("member" in e.lower() for e in errors)

    def test_rejected_on_an_individual_plan(self):
        from store_project.meso.tests.test_agent_validation import make_plan

        plan, _, presc = make_plan()
        raw = adjust_raw(member_id=123, presc=presc, load_pct=90)

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("group" in e.lower() for e in errors)

    def test_swap_honors_the_target_members_contraindication(self):
        coach, group, plan, athletes = make_group_plan()
        ContraindicationFactory(
            athlete=athletes[0], text="L knee — avoid deep knee flexion under load"
        )
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        raw = adjust_raw(
            member_id=m.pk, presc=presc, new_name="Deep Knee Flexion Drill"
        )

        cleaned, errors = validation.clean_change(raw, plan)

        assert cleaned is None
        assert any("contraindication" in e.lower() for e in errors)

    def test_swap_is_screened_per_member_not_folded(self):
        # member[0] flags knee flexion; member[1] does NOT. An adjust that swaps
        # member[1] to a knee-flexion movement is SAFE (it never trains member[0]),
        # proving the backstop is per-member — under the folded group rule (Phase 1)
        # this same swap would be rejected.
        coach, group, plan, athletes = make_group_plan()
        ContraindicationFactory(
            athlete=athletes[0], text="L knee — avoid deep knee flexion under load"
        )
        presc = first_presc(plan)
        safe_member = membership_for(group, athletes[1])
        raw = adjust_raw(
            member_id=safe_member.pk, presc=presc, new_name="Deep Knee Flexion Drill"
        )

        cleaned, errors = validation.clean_change(raw, plan)

        assert errors == []
        assert cleaned["payload"]["swap_name"] == "Deep Knee Flexion Drill"


# --------------------------------------------------------------------------
# Apply — an adjust materializes a real PrescriptionOverride.
# --------------------------------------------------------------------------


class TestAdjustApply:
    def make_batch_with_adjust(self, payload):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        batch = AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction="back off the first member",
            trigger=AgentProposalBatch.Trigger.GROUP,
        )
        change = ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.ADJUST,
            prescription=presc,
            membership=m,
            payload=payload,
        )
        return coach, group, plan, presc, m, batch, change

    def test_apply_creates_the_override(self):
        coach, group, plan, presc, m, batch, change = self.make_batch_with_adjust(
            {"swap_name": "Box Squat", "load_pct": 90}
        )

        result = agent_apply.apply_batch(batch)

        assert result["applied"] == 1
        override = PrescriptionOverride.objects.get(membership=m, prescription=presc)
        assert override.swap_name == "Box Squat"
        assert override.load_pct == 90
        # The shared row itself is untouched — only this member diverges.
        presc.refresh_from_db()
        assert presc.name != "Box Squat"
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED

    def test_apply_upserts_an_existing_override(self):
        coach, group, plan, presc, m, batch, change = self.make_batch_with_adjust(
            {"load_pct": 80}
        )
        m.set_override(presc, swap_name="Old Swap")

        agent_apply.apply_batch(batch)

        override = PrescriptionOverride.objects.get(membership=m, prescription=presc)
        assert override.load_pct == 80

    def test_apply_skips_an_orphaned_membership(self):
        # If the membership was removed between propose and apply, the SET_NULL FK
        # is None → a safe no-op skip (never a crash), like prescription/session.
        coach, group, plan, presc, m, batch, change = self.make_batch_with_adjust(
            {"load_pct": 90}
        )
        change.membership = None
        change.save(update_fields=["membership"])

        result = agent_apply.apply_batch(batch)

        assert result["applied"] == 0
        assert result["skipped"] == 1
        assert not PrescriptionOverride.objects.exists()


# --------------------------------------------------------------------------
# End-to-end — propose → pending batch → apply, through the real endpoints.
# --------------------------------------------------------------------------


class TestAdjustEndToEnd:
    def test_propose_persists_an_adjust_then_apply_overrides(self, client, monkeypatch):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        result = {
            "summary": "Backed off one member's squat.",
            "changes": [
                adjust_raw(
                    member_id=m.pk, presc=presc, new_name="", load_pct=85, new_sets=""
                )
            ],
        }
        install_fake(monkeypatch, result)
        client.force_login(coach)

        batch_id = propose(client, plan, "ease off the first athlete").json()[
            "batch_id"
        ]
        data = client.get(status_url(batch_id)).json()
        assert data["status"] == AgentProposalBatch.Status.PENDING
        assert len(data["changes"]) == 1

        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch_id})
        )
        assert resp.status_code == 200
        override = PrescriptionOverride.objects.get(membership=m, prescription=presc)
        assert override.load_pct == 85


# --------------------------------------------------------------------------
# Client tool + framing — the model is told it CAN adjust per member.
# --------------------------------------------------------------------------


class TestAdjustClientSchema:
    def test_tool_offers_the_adjust_kind_and_member_fields(self):
        schema = agent_client.PROPOSE_TOOL["input_schema"]
        item = schema["properties"]["changes"]["items"]["properties"]
        assert "adjust" in item["kind"]["enum"]
        assert "member_id" in item
        assert "load_pct" in item

    def test_group_framing_explains_per_athlete_adjusts(self):
        context = {"group": {"name": "Squad", "members": []}}
        prompt = agent_client._user_prompt(context, "ease off Devon's squat")
        assert "adjust" in prompt.lower()
        assert "member_id" in prompt


# --------------------------------------------------------------------------
# Review serialization — the coach sees WHICH member an adjust targets.
# --------------------------------------------------------------------------


class TestAdjustReviewSerialization:
    def test_serialized_change_names_the_member(self):
        from store_project.meso.serializers import serialize_proposed_change

        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        m = membership_for(group, athletes[0])
        batch = AgentProposalBatch.objects.create(
            plan=plan, coach=coach, instruction="x"
        )
        change = ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.ADJUST,
            prescription=presc,
            membership=m,
            payload={"load_pct": 90},
        )

        data = serialize_proposed_change(change)

        assert data["member"] == athletes[0].display_name()

    def test_non_adjust_change_has_no_member(self):
        from store_project.meso.serializers import serialize_proposed_change

        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        batch = AgentProposalBatch.objects.create(
            plan=plan, coach=coach, instruction="x"
        )
        change = ProposedChangeFactory(
            batch=batch, kind=ProposedChange.Kind.SWAP, prescription=presc
        )

        assert serialize_proposed_change(change)["member"] == ""
