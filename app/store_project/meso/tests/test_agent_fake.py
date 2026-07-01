"""Demo/sandbox mode (#388/#389) — a pre-baked agent client, no Anthropic call.

``MESO_AGENT_FAKE`` swaps ``get_default_client`` for ``agent.fake.FakeDemoClient``
so a recorded walkthrough video / a public sandbox never needs a real API key and
never touches the network. It runs through the exact same pipeline as the real
client (``service.run_proposal_job`` → ``validation.clean_change`` → the review
gate), so these tests exercise it end-to-end rather than mocking the seams.
"""

import json

import pytest
from django.test import override_settings
from django.urls import reverse

from store_project.meso.agent import client as client_module
from store_project.meso.agent import service
from store_project.meso.agent.fake import FakeDemoClient
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


# -- get_default_client wiring ----------------------------------------------


class TestGetDefaultClientFake:
    @override_settings(MESO_AGENT_FAKE=True, ANTHROPIC_API_KEY="")
    def test_fake_setting_wins_even_with_no_api_key(self):
        client = client_module.get_default_client()
        assert isinstance(client, FakeDemoClient)

    @override_settings(MESO_AGENT_FAKE=True, ANTHROPIC_API_KEY="sk-ant-real-key")
    def test_fake_setting_wins_over_a_real_api_key(self):
        # The demo mode must not accidentally spend real API credits.
        client = client_module.get_default_client()
        assert isinstance(client, FakeDemoClient)

    @override_settings(MESO_AGENT_FAKE=False, ANTHROPIC_API_KEY="")
    def test_default_behavior_unchanged_no_key_no_client(self):
        assert client_module.get_default_client() is None

    @override_settings(MESO_AGENT_FAKE=False, ANTHROPIC_API_KEY="sk-ant-real-key")
    def test_default_behavior_unchanged_with_key_returns_real_client(self):
        client = client_module.get_default_client()
        assert type(client).__name__ == "MesoAgentClient"
        assert not isinstance(client, FakeDemoClient)


# -- FakeDemoClient.propose ---------------------------------------------------


class TestFakeDemoClientPropose:
    def test_targets_real_ids_and_passes_validation_end_to_end(self):
        plan, session, presc = make_plan()
        fake = FakeDemoClient()

        batch, rejected = service.propose_changes(
            plan, "Draft something for this week.", coach=plan.coach, client=fake
        )

        assert rejected == []
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.changes.count() > 0
        assert batch.model == "meso-fake-demo"
        # No network call happened, so the usage ledger is honestly empty.
        assert batch.input_tokens == 0
        assert batch.output_tokens == 0
        assert batch.api_calls == 0
        for change in batch.changes.all():
            if change.kind in ("swap", "progress"):
                assert change.prescription_id == presc.pk
            if change.kind == "volume":
                assert change.session_id is not None

    def test_no_targets_yields_empty_changes(self):
        context = {"plan": {"program": []}}
        result = FakeDemoClient().propose(context=context, instruction="go")
        assert result["changes"] == []
        assert result["summary"]

    def test_contraindication_case_still_yields_a_persisted_change(self):
        plan, session, presc = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete,
            text="L knee — avoid deep knee flexion under load",
        )
        fake = FakeDemoClient()

        batch, rejected = service.propose_changes(
            plan, "Draft something for this week.", coach=plan.coach, client=fake
        )

        assert batch.changes.count() >= 1
        # The demo never proposes the forbidden movement in the first place.
        for change in batch.changes.all():
            assert "flexion" not in (change.after or "").lower()
            assert "flexion" not in (change.introduces_exercise or "").lower()
            assert "flexion" not in change.payload.get("name", "").lower()

    def test_summary_reads_like_a_coach_not_a_test_fixture(self):
        plan, _, _ = make_plan()
        result = FakeDemoClient().propose(
            context=service.build_context(plan), instruction="go"
        )
        summary = result["summary"].lower()
        assert "scripted" not in summary
        assert "fake" not in summary

    def test_swap_skips_the_row_already_tagged_safe(self):
        # The demo data's day 1 leads with a curated knee-safe row; swapping THAT
        # row would undercut the honors line beside it. The swap should land on
        # the first untagged row instead (the one the contraindication is about).
        context = {
            "plan": {
                "program": [
                    {
                        "id": 1,
                        "name": "Lower",
                        "exercises": [
                            {"id": 11, "name": "Box Squat", "tag": "knee-safe"},
                            {"id": 12, "name": "Bulgarian Split Squat"},
                        ],
                    }
                ]
            },
            "athlete": {
                "contraindications": ["L knee — avoid deep knee flexion under load"]
            },
        }
        result = FakeDemoClient().propose(context=context, instruction="go")
        swap = next(c for c in result["changes"] if c["kind"] == "swap")
        assert swap["prescription_id"] == 12
        # …and the honors line quotes the plan's real contraindication, not a stub.
        assert swap["honors"] == "L knee — avoid deep knee flexion under load"
        # The progression then targets a row other than the swapped one.
        progress = next(c for c in result["changes"] if c["kind"] == "progress")
        assert progress["prescription_id"] == 11

    def test_honors_picks_the_contraindication_the_instruction_is_about(self):
        # Maya's demo card lists an unrelated flag first; the coach's knee-themed
        # instruction should pull the knee flag into the honors chip, not
        # whatever happens to be first in the list.
        context = {
            "plan": {
                "program": [
                    {
                        "id": 1,
                        "name": "Lower",
                        "exercises": [{"id": 11, "name": "Bulgarian Split Squat"}],
                    }
                ]
            },
            "athlete": {
                "contraindications": [
                    "No max-effort jumping / impact",
                    "L knee — avoid deep knee flexion under load",
                ]
            },
        }
        result = FakeDemoClient().propose(
            context=context,
            instruction="Her left knee has been cranky — keep this week knee-friendly.",
        )
        swap = next(c for c in result["changes"] if c["kind"] == "swap")
        assert swap["honors"] == "L knee — avoid deep knee flexion under load"

    def test_every_change_fills_the_before_after_row(self):
        # The review card renders its strikethrough → arrow row unconditionally;
        # an empty pair shows a dangling arrow on camera.
        context = {
            "plan": {
                "program": [
                    {
                        "id": 1,
                        "name": "Lower",
                        "exercises": [
                            {"id": 11, "name": "Back Squat", "load": "100"},
                            {"id": 12, "name": "Leg Press", "load": "110"},
                        ],
                    },
                    {
                        "id": 2,
                        "name": "Upper",
                        "exercises": [{"id": 21, "name": "Bench Press", "sets": "4"}],
                    },
                ]
            }
        }
        result = FakeDemoClient().propose(context=context, instruction="go")
        assert len(result["changes"]) == 3
        for change in result["changes"]:
            assert change["after"], change
        volume = next(c for c in result["changes"] if c["kind"] == "volume")
        assert volume["before"] == "4 sets"
        assert volume["after"] == "3 sets"
        assert volume["new_sets"] == "3"

    def test_abs_progression_stays_a_bare_number(self):
        # ``apply`` writes new_load verbatim into the load column, where every
        # existing row is unitless — a "kg" suffix would be the one odd cell in
        # the on-camera designer grid.
        context = {
            "plan": {
                "program": [
                    {
                        "id": 1,
                        "name": "Lower",
                        "exercises": [
                            {"id": 11, "name": "Hip Thrust"},
                            {"id": 12, "name": "Leg Press", "load": "110"},
                        ],
                    }
                ]
            }
        }
        result = FakeDemoClient().propose(context=context, instruction="go")
        progress = next(c for c in result["changes"] if c["kind"] == "progress")
        assert progress["new_load"] == "112.5"


# -- end-to-end view test -----------------------------------------------------


class TestAgentProposeEndpointFake:
    @override_settings(
        MESO_AGENT_FAKE=True, ANTHROPIC_API_KEY="", MESO_AGENT_RUN_SYNC=True
    )
    def test_propose_endpoint_resolves_to_a_reviewable_pending_batch(self, client):
        plan, _, _ = make_plan()
        client.force_login(plan.coach)

        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"instruction": "Draft this week."}),
            content_type="application/json",
        )

        assert resp.status_code == 202
        data = resp.json()
        batch = AgentProposalBatch.objects.get(pk=data["batch_id"])
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.changes.count() > 0

        status_resp = client.get(
            reverse("meso:api_batch_status", kwargs={"batch_id": batch.pk})
        )
        status_data = status_resp.json()
        assert status_data["status"] == AgentProposalBatch.Status.PENDING
        assert len(status_data["changes"]) > 0
        assert "review_url" in status_data
