"""#732 — the agent only ever targets a row's line-0 prescription cell.

A ``prescription_id`` naming a sub-line (a cue, or the athlete's / coach's set
line) is refused by validation, and an already-persisted change that names one is
skipped at apply time, so the agent can never rewrite a performance line.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.agent import validation
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_agent_validation import base_change
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def _plan_with_athlete_line():
    plan, _, cell = make_plan()
    cell.text = "3 x 5, 200"
    cell.save(update_fields=["text"])
    line1 = sub_line(cell, "225 x 5", athlete_authored=True)
    assert line1.line == 1
    return plan, cell, line1


def _clean(plan, **overrides):
    return validation.clean_change(
        base_change(kind="progress", **overrides),
        plan,
        mesocycle=plan.mesocycles.first(),
    )


class TestValidation:
    def test_sub_line_target_refused(self):
        plan, _, line1 = _plan_with_athlete_line()
        cleaned, errors = _clean(plan, prescription_id=line1.pk, new_load="230")
        assert cleaned is None
        assert any("is a sub-line" in e for e in errors), errors

    def test_line0_target_still_validates(self):
        plan, cell, _ = _plan_with_athlete_line()
        cleaned, errors = _clean(plan, prescription_id=cell.pk, new_load="205")
        assert errors == []
        assert cleaned["prescription"] == cell


class TestApply:
    def _batch(self, plan, presc):
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        change = ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.PROGRESS,
            prescription=presc,
            payload={"load": "230"},
        )
        return batch, change

    def test_persisted_sub_line_change_is_skipped(self, client):
        plan, _, line1 = _plan_with_athlete_line()
        logged = LoggedSetFactory(prescription=line1)
        batch, _ = self._batch(plan, line1)
        client.force_login(plan.coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == 0
        line1.refresh_from_db()
        assert line1.text == "225 x 5"
        assert line1.athlete_authored is True
        logged.refresh_from_db()
        assert (logged.prescription_id, logged.reps) == (line1.pk, "10")
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED

    def test_line0_change_still_applies(self, client):
        plan, cell, line1 = _plan_with_athlete_line()
        batch, _ = self._batch(plan, cell)
        client.force_login(plan.coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert json.loads(resp.content)["applied"] == 1
        cell.refresh_from_db()
        line1.refresh_from_db()
        assert "230" in cell.text
        assert line1.text == "225 x 5"
