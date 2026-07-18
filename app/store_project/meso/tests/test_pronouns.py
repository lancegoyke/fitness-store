"""Issue #441 P3-4 — pronoun de-hardcoding on the deliver + review screens.

Both screens only carry ``{{ athlete.name }}`` in their presenter context (no
``is_self``/gender), so the fix swaps the hard-coded she/her copy for name
interpolation + singular they/them/their — no presenter plumbing. These tests
GET each *real* rendered page for a delivered plan / a proposed-change batch and
assert the neutral phrasing is present and the gendered phrasing is gone.

Pre-implementation this is RED: ``deliver.html``/``review.html`` still say
"she"/"her" today, so the neutral assertions fail (and the gendered ones would
fail once the templates are fixed).
"""

import pytest
from django.urls import reverse

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

from ._helpers import day

pytestmark = pytest.mark.django_db

# A gender-neutral athlete name (no "she"/"her"/"his" substrings) so the
# interpolated-name assertions can't be satisfied by coincidence. Rendered by
# ``athlete.name`` (== ``user.display_name()``) on both screens.
ATHLETE_NAME = "Robin Alvarez"


def _plan(athlete_name=ATHLETE_NAME):
    """A minimal owned, active plan with one current week + day."""
    rel = CoachAthleteFactory(athlete=UserFactory(name=athlete_name))
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week1 = WeekFactory(mesocycle=meso, index=1)
    day(week1, day_number=1, name="Lower")
    return plan, meso, week1


class TestDeliverPronouns:
    def _get(self, client, plan, **params):
        url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        return client.get(url, params)

    def test_head_and_success_copy_are_gender_neutral(self, client):
        # NB: the live+notify deliver copy (2d) is what's rendered here — the
        # head ("your edits are already live in NAME's app") and the
        # post-deliver success ("Block delivered … got the heads-up … on their
        # phone"). This test pins that all of it stays gender-neutral (name +
        # they/their).
        plan, _, _ = _plan()
        client.force_login(plan.relationship.coach)

        body = self._get(client, plan).content.decode()

        # Neutral phrasing (name + they/their) is present…
        assert f"already live in {ATHLETE_NAME}'s app" in body
        assert f"Block delivered to {ATHLETE_NAME}" in body
        assert "on their phone" in body
        assert "Track sessions" in body
        # …and the hard-coded she/her copy is gone.
        assert "in her app" not in body
        assert "She'll see all" not in body
        assert "on her phone" not in body
        assert "Track her sessions" not in body


class TestReviewPronouns:
    def _batch(self):
        rel = CoachAthleteFactory(athlete=UserFactory(name=ATHLETE_NAME))
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.relationship.coach)
        ProposedChangeFactory(batch=batch)
        return plan, batch

    def test_grounding_line_is_gender_neutral(self, client):
        plan, batch = self._batch()
        client.force_login(plan.relationship.coach)

        url = reverse("meso:review_batch", kwargs={"batch_id": batch.pk})
        body = client.get(url).content.decode()

        # "Grounded on <name>'s profile, contraindications, …"
        assert f"{ATHLETE_NAME}'s profile" in body
        assert "Grounded on her profile" not in body
