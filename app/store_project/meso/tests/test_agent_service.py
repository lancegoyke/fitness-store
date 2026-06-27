"""Agent slice Phase 1 — the service that orchestrates one agent run.

``service.propose_changes`` grounds the request on the plan + contraindications +
coach style, calls the client (injected here as a fake so tests never touch the
network), runs every candidate through the validation guardrail, and persists a
batch with only the clean changes. Rejected candidates are returned for logging,
never persisted — the review screen only ever sees safe, in-plan edits.
"""

import pytest

from store_project.meso.agent import client as client_module
from store_project.meso.agent import service
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


class FakeClient:
    """Stands in for ``MesoAgentClient`` — returns a canned tool result."""

    model = "claude-opus-4-8-test"

    def __init__(self, result):
        self._result = result
        self.context = None
        self.instruction = None

    def propose(self, *, context, instruction):
        self.context = context
        self.instruction = instruction
        return self._result


def test_persists_valid_changes_into_a_batch():
    plan, session, presc = make_plan()
    fake = FakeClient(
        {
            "summary": "Swapped the squat and progressed the deadlift.",
            "changes": [
                {
                    "kind": "swap",
                    "prescription_id": presc.pk,
                    "day_label": "Day 1 · Lower",
                    "title": "Back Squat → Box Squat",
                    "before": "Back Squat",
                    "after": "Box Squat",
                    "rationale": "Shorter range of motion.",
                    "honors": "L knee",
                    "introduces_exercise": "Box Squat",
                },
                {
                    "kind": "progress",
                    "session_id": session.pk,
                    "title": "Trap-Bar Deadlift → 92.5 kg",
                    "rationale": "Anchored to last block.",
                },
            ],
        }
    )

    batch, rejected = service.propose_changes(
        plan, "Make it knee-safe.", coach=plan.coach, client=fake
    )

    assert rejected == []
    assert batch.summary.startswith("Swapped")
    assert batch.model == "claude-opus-4-8-test"
    assert batch.status == AgentProposalBatch.Status.PENDING
    changes = list(batch.changes.all())
    assert len(changes) == 2
    assert changes[0].kind == "swap"
    assert changes[0].prescription == presc
    assert changes[0].order == 0
    assert changes[1].order == 1


def test_grounds_the_client_on_plan_and_contraindications():
    plan, _, _ = make_plan()
    ContraindicationFactory(
        athlete=plan.athlete, text="L knee — avoid deep knee flexion"
    )
    CoachProfileFactory(user=plan.coach, programming_style=["Compound-first"])
    fake = FakeClient({"summary": "", "changes": []})

    service.propose_changes(plan, "Plan week 2.", coach=plan.coach, client=fake)

    assert fake.instruction == "Plan week 2."
    assert fake.context["plan"]["plan"]["id"] == plan.pk
    assert (
        "L knee — avoid deep knee flexion"
        in fake.context["athlete"]["contraindications"]
    )
    assert "Compound-first" in fake.context["coach_style"]["tags"]


def test_unsafe_change_is_rejected_not_persisted():
    plan, _, presc = make_plan()
    ContraindicationFactory(
        athlete=plan.athlete, text="L knee — avoid deep knee flexion under load"
    )
    fake = FakeClient(
        {
            "summary": "",
            "changes": [
                {
                    "kind": "swap",
                    "prescription_id": presc.pk,
                    "title": "Back Squat → Deep Knee Flexion Drill",
                    "rationale": "...",
                    "introduces_exercise": "Deep Knee Flexion Drill",
                }
            ],
        }
    )

    batch, rejected = service.propose_changes(plan, "go", coach=plan.coach, client=fake)

    assert batch.changes.count() == 0
    assert len(rejected) == 1
    assert any("contraindication" in e for e in rejected[0]["errors"])


def test_foreign_target_is_rejected_not_persisted():
    plan, _, _ = make_plan()
    other_plan, _, other_presc = make_plan()
    fake = FakeClient(
        {
            "summary": "",
            "changes": [
                {
                    "kind": "swap",
                    "prescription_id": other_presc.pk,
                    "title": "Tamper with another plan",
                    "rationale": "...",
                }
            ],
        }
    )

    batch, rejected = service.propose_changes(plan, "go", coach=plan.coach, client=fake)

    assert batch.changes.count() == 0
    assert len(rejected) == 1
    assert not ProposedChange.objects.filter(prescription=other_presc).exists()


def test_raises_when_no_client_configured(monkeypatch):
    plan, _, _ = make_plan()
    monkeypatch.setattr(client_module, "get_default_client", lambda: None)
    with pytest.raises(service.AgentNotConfigured):
        service.propose_changes(plan, "go", coach=plan.coach)


class BoomClient:
    """A client whose provider call fails (timeout / API error / bad model)."""

    model = "claude-opus-4-8-test"

    def propose(self, *, context, instruction):
        raise RuntimeError("provider is down")


def test_provider_failure_wrapped_as_agent_error():
    plan, _, _ = make_plan()
    with pytest.raises(service.AgentError):
        service.propose_changes(plan, "go", coach=plan.coach, client=BoomClient())
    assert not AgentProposalBatch.objects.exists()
