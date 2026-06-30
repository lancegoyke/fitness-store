"""Capturing per-run token usage + cost on the batch (agent-usage tracking v1).

``AgentProposalBatch`` is the per-run usage ledger: the real client captures the
Claude ``usage`` block + ``_request_id`` at the call site, and the service threads
it (plus the wall-clock duration, the estimated cost, the ``trigger``, and the
coach's ``billing_status`` snapshot) onto the batch. Captured on the success path
and — model + duration — on a failed run too (U5). The Anthropic invoice stays the
billing source of truth; our number is an internal estimate for margin +
attribution. See ``docs/meso/agent-usage-plan.md``.
"""

from decimal import Decimal

import pytest

from store_project.meso.agent import client as client_module
from store_project.meso.agent import evals
from store_project.meso.agent import service
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


# --- fakes for the anthropic SDK boundary ---------------------------------


class FakeUsage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)


class FakeToolBlock:
    type = "tool_use"
    name = client_module.TOOL_NAME

    def __init__(self, data):
        self.input = data


class FakeMessage:
    def __init__(self, *, data, usage, request_id="req_abc", stop_reason="tool_use"):
        self.content = [FakeToolBlock(data)]
        self.usage = usage
        self._request_id = request_id
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, message):
        self._message = message
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._message


class FakeAnthropic:
    def __init__(self, message):
        self.messages = FakeMessages(message)


class UsageClient:
    """A fake whose ``propose`` returns a ``ProposalResult`` carrying usage."""

    model = "claude-opus-4-8"

    def __init__(self, usage, data=None):
        self._usage = usage
        self._data = data or {"summary": "done", "changes": []}

    def propose(self, *, context, instruction):
        return client_module.ProposalResult(data=self._data, usage=self._usage)


class BoomClient:
    model = "claude-opus-4-8-test"

    def propose(self, *, context, instruction):
        raise RuntimeError("provider is down")


class DictClient:
    """A legacy fake returning a bare dict (no usage) — the scripted-client path."""

    model = "claude-opus-4-8-test"

    def propose(self, *, context, instruction):
        return {"summary": "", "changes": []}


# --- client captures usage from the SDK response --------------------------


class TestClientCapturesUsage:
    def _client_with(self, message):
        c = client_module.MesoAgentClient(api_key="x", model="claude-opus-4-8")
        c._client = FakeAnthropic(message)
        return c

    def test_propose_returns_a_proposal_result_with_usage(self):
        message = FakeMessage(
            data={"summary": "ok", "changes": []},
            usage=FakeUsage(
                input_tokens=1200,
                output_tokens=800,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=4000,
            ),
            request_id="req_xyz",
            stop_reason="tool_use",
        )
        result = self._client_with(message).propose(context={}, instruction="go")

        assert isinstance(result, client_module.ProposalResult)
        assert result.data == {"summary": "ok", "changes": []}
        assert result.usage.input_tokens == 1200
        assert result.usage.output_tokens == 800
        assert result.usage.cache_creation_input_tokens == 500
        assert result.usage.cache_read_input_tokens == 4000
        assert result.usage.request_id == "req_xyz"
        assert result.usage.stop_reason == "tool_use"
        assert result.usage.api_calls == 1

    def test_missing_cache_fields_default_to_zero(self):
        # A response that used no prompt caching omits the cache token attrs.
        usage = FakeUsage(input_tokens=10, output_tokens=5)
        del usage.cache_creation_input_tokens
        del usage.cache_read_input_tokens
        message = FakeMessage(data={"summary": "", "changes": []}, usage=usage)
        result = self._client_with(message).propose(context={}, instruction="go")
        assert result.usage.cache_creation_input_tokens == 0
        assert result.usage.cache_read_input_tokens == 0

    def test_no_tool_block_still_returns_usage(self):
        message = FakeMessage(
            data={"summary": "", "changes": []},
            usage=FakeUsage(input_tokens=7, output_tokens=3),
        )
        message.content = []  # model returned no tool_use block
        result = self._client_with(message).propose(context={}, instruction="go")
        assert result.data == {"summary": "", "changes": []}
        assert result.usage.input_tokens == 7


def test_normalize_result_coerces_bare_dict_and_passthrough():
    pr = client_module.ProposalResult(data={"a": 1})
    assert client_module.normalize_result(pr) is pr
    assert client_module.normalize_result({"a": 1}).data == {"a": 1}
    assert client_module.normalize_result({"a": 1}).usage == client_module.RunUsage()
    assert client_module.normalize_result(None).data == {}


# --- service threads usage onto the batch (success path) ------------------


class TestPersistUsageOnSuccess:
    def test_run_writes_usage_cost_and_duration(self):
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        usage = client_module.RunUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=3000,
            request_id="req_run",
            stop_reason="tool_use",
        )

        service.run_proposal_job(batch.pk, client=UsageClient(usage))

        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.model == "claude-opus-4-8"
        assert batch.input_tokens == 1000
        assert batch.output_tokens == 500
        assert batch.cache_creation_input_tokens == 200
        assert batch.cache_read_input_tokens == 3000
        assert batch.api_calls == 1
        assert batch.request_id == "req_run"
        assert batch.stop_reason == "tool_use"
        assert batch.duration_ms is not None and batch.duration_ms >= 0
        # (1000*5 + 500*25 + 200*6.25 + 3000*0.50) / 1e6
        assert batch.estimated_cost_usd == Decimal("0.020250")

    def test_sync_path_also_captures_usage(self):
        plan, _, _ = make_plan()
        usage = client_module.RunUsage(input_tokens=100, output_tokens=50)
        batch, _ = service.propose_changes(
            plan, "go", coach=plan.coach, client=UsageClient(usage)
        )
        batch.refresh_from_db()
        assert batch.input_tokens == 100
        assert batch.output_tokens == 50
        assert batch.estimated_cost_usd == Decimal("0.001750")  # (100*5+50*25)/1e6

    def test_bare_dict_client_records_zero_usage_and_unknown_cost(self):
        # A legacy/scripted client returning a plain dict made no real API call.
        plan, _, _ = make_plan()
        batch, _ = service.propose_changes(
            plan, "go", coach=plan.coach, client=DictClient()
        )
        batch.refresh_from_db()
        assert batch.input_tokens == 0
        assert batch.output_tokens == 0
        # DictClient.model isn't in the rate table → cost is None, not a wrong 0.
        assert batch.estimated_cost_usd is None


# --- failure path still attributes (U5) -----------------------------------


def test_failed_run_records_model_and_duration_with_zero_usage():
    plan, _, _ = make_plan()
    batch = service.create_drafting_batch(plan, "go", coach=plan.coach)

    service.run_proposal_job(batch.pk, client=BoomClient())

    batch.refresh_from_db()
    assert batch.status == AgentProposalBatch.Status.FAILED
    assert "provider is down" in batch.error
    # A failed run still attributes by model + latency for the usage report (U5).
    assert batch.model == "claude-opus-4-8-test"
    assert batch.duration_ms is not None
    # No usage block came back, so tokens/cost stay at their defaults.
    assert batch.input_tokens == 0
    assert batch.estimated_cost_usd is None


# --- trigger + billing_status snapshots -----------------------------------


class TestRunDimensions:
    def test_manual_run_defaults_to_manual_trigger(self):
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        assert batch.trigger == AgentProposalBatch.Trigger.MANUAL

    def test_draft_trigger_is_snapshotted(self):
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(
            plan, "go", coach=plan.coach, trigger=AgentProposalBatch.Trigger.DRAFT
        )
        assert batch.trigger == AgentProposalBatch.Trigger.DRAFT

    def test_eval_run_is_tagged_eval(self):
        # The golden-eval harness must tag its runs ``eval`` so the usage report
        # can exclude them from real cost (they're a quality check, not coach use).
        plan, _, presc = make_plan()
        case = evals.GOLDEN_CASES[0]
        evals.evaluate(plan, case, client=evals.ScriptedEvalClient())
        batch = plan.proposal_batches.get()
        assert batch.trigger == AgentProposalBatch.Trigger.EVAL

    def test_comped_coach_billing_status_snapshot(self):
        # make_plan comps the coach, so the run records the comped tier.
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        assert batch.billing_status == CoachSubscription.Status.COMPED

    def test_free_coach_with_no_subscription_snapshots_free(self):
        # A plan whose coach has no subscription row reads as the free tier.
        plan = PlanFactory()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        assert batch.billing_status == CoachSubscription.Status.FREE

    def test_active_coach_billing_status_snapshot(self):
        plan = PlanFactory()
        CoachSubscriptionFactory(
            coach=plan.coach, status=CoachSubscription.Status.ACTIVE
        )
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        assert batch.billing_status == CoachSubscription.Status.ACTIVE
