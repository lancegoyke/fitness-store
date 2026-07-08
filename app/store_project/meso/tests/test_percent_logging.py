"""Units & RPE/%1RM slice (S2) Phase 2b — athlete %1RM logging ergonomics.

Phase 1 gave the prescription a first-class ``load_type`` (``abs``/``pct``) and the
athlete already sees a ``%`` target (``_target_label``). The remaining gap is
*ergonomics*: a %1RM target is an intensity, not a weight, so the athlete still
has to convert "75%" into a bar load by hand. This phase threads the structured
load type + the plan's unit into the logger payload so the client can offer an
estimated-1RM helper (% ⇄ load). The maths itself lives in ``meso_athlete.js``
(Vitest); these tests pin the **data contract** the client hydrates from.

No model change — a ``LoggedSet`` still records the *actual* (absolute) weight,
and the athlete's estimated 1RM lives client-side (localStorage), the same
"reuse what exists, defer new tables" taste as the offline log queue.
"""

import pytest

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import LoadType
from store_project.meso.models import Plan
from store_project.meso.models import Unit
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_session(unit=Unit.KILOGRAMS):
    """A delivered-shape session with one absolute + one %1RM prescription."""
    rel = CoachAthleteFactory(coach=UserFactory(), athlete=UserFactory())
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE, unit=unit)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    absolute = presc(session, name="Back Squat", sets="4", reps="6", load="70", order=0)
    percent = presc(
        session,
        name="Front Squat",
        sets="3",
        reps="5",
        load="75",
        load_type=LoadType.PERCENT,
        order=1,
    )
    return plan, session, absolute, percent


class TestAthleteSessionUnit:
    def test_session_ctx_carries_plan_unit(self):
        plan, session, _, _ = seed_session(unit=Unit.POUNDS)
        ctx = presenters.athlete_session(session, plan.relationship.athlete)
        assert ctx["unit"] == Unit.POUNDS


class TestLogPayloadLoadType:
    def test_payload_carries_unit(self):
        plan, session, _, _ = seed_session()
        ctx = presenters.athlete_session(session, plan.relationship.athlete)
        payload = presenters.athlete_log_payload(ctx)
        assert payload["unit"] == Unit.KILOGRAMS

    def test_payload_exercises_carry_load_and_load_type(self):
        plan, session, absolute, percent = seed_session()
        ctx = presenters.athlete_session(session, plan.relationship.athlete)
        payload = presenters.athlete_log_payload(ctx)
        by_id = {e["id"]: e for e in payload["exercises"]}

        abs_row = by_id[absolute.pk]
        assert abs_row["load"] == "70"
        assert abs_row["load_type"] == LoadType.ABSOLUTE

        pct_row = by_id[percent.pk]
        assert pct_row["load"] == "75"
        assert pct_row["load_type"] == LoadType.PERCENT
