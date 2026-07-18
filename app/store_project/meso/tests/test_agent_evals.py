"""Agent slice Phase 4 — the golden eval harness.

The eval corpus checks model-agnostic invariants (responsive / grounded / safe)
so agent quality doesn't silently regress. These run the corpus through scripted
clients (no network / key) to cover the checks and to prove the deterministic
guardrail holds end-to-end: an unsafe model output is rejected before it can
reach a persisted change, so it surfaces as a *responsiveness* failure, never a
*safety* leak.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from store_project.meso.agent import client as client_module
from store_project.meso.agent import evals
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.tests.test_agent_service import FakeClient
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


class TestCheckResult:
    def test_too_few_changes_is_a_failure(self):
        plan, _, _ = make_plan()
        # An empty batch against a case that expects >= 1 change fails the
        # responsive invariant.
        batch, rejected = evals.service.propose_changes(
            plan,
            "go",
            coach=plan.coach,
            mesocycle=plan.mesocycles.first(),
            client=FakeClient({"summary": "", "changes": []}),
        )
        case = evals.GoldenCase(name="x", instruction="go", min_changes=1)
        failures, _ = evals.check_result(case, plan, batch, rejected)
        assert any("at least 1" in f for f in failures)

    def test_safe_grounded_batch_has_no_failures(self):
        plan, _, presc = make_plan()
        result = {
            "summary": "ok",
            "changes": [
                {
                    "kind": "progress",
                    "prescription_id": presc.pk,
                    "title": "Back Squat → 82.5 kg",
                    "rationale": "small step",
                    "new_load": "82.5 kg",
                }
            ],
        }
        batch, rejected = evals.service.propose_changes(
            plan,
            "progress",
            coach=plan.coach,
            mesocycle=plan.mesocycles.first(),
            client=FakeClient(result),
        )
        case = evals.GoldenCase(
            name="progress",
            instruction="progress",
            expect_kinds=frozenset({"progress"}),
        )
        failures, warnings = evals.check_result(case, plan, batch, rejected)
        assert failures == []
        assert warnings == []


class TestGuardrailHoldsEndToEnd:
    def test_unsafe_swap_never_persists_so_safety_never_fails(self):
        plan, _, presc = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete, text="L knee — avoid deep knee flexion under load"
        )
        unsafe = {
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
        batch, rejected = evals.service.propose_changes(
            plan,
            "swap",
            coach=plan.coach,
            mesocycle=plan.mesocycles.first(),
            client=FakeClient(unsafe),
        )
        case = evals.GoldenCase(
            name="knee_safe_swap", instruction="swap", expect_kinds=frozenset({"swap"})
        )
        failures, _ = evals.check_result(case, plan, batch, rejected)

        # The unsafe swap was rejected (not persisted) ...
        assert batch.changes.count() == 0
        assert len(rejected) == 1
        # ... so the only failure is responsiveness, never a safety leak.
        assert any("at least 1" in f for f in failures)
        assert not any("forbidden" in f for f in failures)


class TestScriptedCorpus:
    def test_every_golden_case_passes_under_the_scripted_client(self):
        # One rich scenario serves the whole corpus; the scripted client picks a
        # safe edit matching each instruction's intent.
        plan, _, _ = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete, text="L knee — avoid deep knee flexion under load"
        )
        client = evals.ScriptedEvalClient()

        for case in evals.GOLDEN_CASES:
            result = evals.evaluate(
                plan, case, client=client, mesocycle=plan.mesocycles.first()
            )
            assert result.passed, f"{case.name} failed: {result.failures}"
            assert result.n_changes >= case.min_changes


class TestEvalCommand:
    def test_dry_run_passes_against_a_plan(self):
        plan, _, _ = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete, text="L knee — avoid deep knee flexion under load"
        )
        out = io.StringIO()
        call_command("meso_agent_eval", dry_run=True, plan_id=plan.pk, stdout=out)
        output = out.getvalue()
        assert "All cases passed." in output
        # Side-effect-free: the run is rolled back, nothing persisted.
        from store_project.meso.models import AgentProposalBatch

        assert not AgentProposalBatch.objects.exists()

    def test_skips_cleanly_without_a_key(self, monkeypatch):
        plan, _, _ = make_plan()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        out = io.StringIO()
        # No key + not --dry-run → a clean skip, not an error.
        call_command("meso_agent_eval", plan_id=plan.pk, stdout=out)
        assert "ANTHROPIC_API_KEY is not set" in out.getvalue()

    def test_errors_when_no_plan_exists(self):
        with pytest.raises(CommandError):
            call_command("meso_agent_eval", dry_run=True)
