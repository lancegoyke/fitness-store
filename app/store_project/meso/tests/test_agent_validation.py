"""Agent slice Phase 1 — the deterministic validation guardrail.

``agent.validation`` is the server-side backstop B6 calls for: contraindications
are enforced here, not just in the prompt. Two layers — structural (targets must
belong to the plan; valid kind; sane fields) and a contraindication backstop (a
swap may not re-introduce a flagged movement). The service runs every candidate
the model returns through this before persisting anything.
"""

import pytest

from store_project.meso.agent import validation
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_plan(athlete=None):
    rel = CoachAthleteFactory(athlete=athlete or UserFactory())
    plan = PlanFactory(relationship=rel)
    meso = MesocycleFactory(plan=plan, order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = SessionFactory(week=week, day_number=1, name="Lower")
    presc = ExercisePrescriptionFactory(session=session, name="Back Squat")
    return plan, session, presc


def base_change(**overrides):
    change = {
        "kind": "swap",
        "day_label": "Day 1 · Lower",
        "title": "Back Squat → Box Squat",
        "before": "Back Squat",
        "after": "Box Squat",
        "rationale": "Shorter range.",
        "honors": "",
        "introduces_exercise": "",
    }
    change.update(overrides)
    return change


class TestForbiddenTerms:
    def test_extracts_avoid_clause_terms(self):
        plan, _, _ = make_plan()
        athlete = plan.athlete
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        ContraindicationFactory(athlete=athlete, text="No max-effort jumping / impact")
        terms = validation.forbidden_terms(plan)
        assert "flexion" in terms
        assert "jumping" in terms
        # Short / generic words are filtered out.
        assert "knee" not in terms
        assert "under" not in terms

    def test_inactive_contraindications_are_ignored(self):
        plan, _, _ = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete,
            text="Lower back — no conventional pull",
            active=False,
        )
        assert "conventional" not in validation.forbidden_terms(plan)


class TestCleanChange:
    def test_valid_swap_targeting_a_plan_prescription(self):
        plan, session, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk), plan
        )
        assert errors == []
        assert cleaned["kind"] == "swap"
        assert cleaned["prescription"] == presc
        # A prescription backfills its session for display/apply.
        assert cleaned["session"] == session
        # The model's explanation is preserved for the review screen.
        assert cleaned["rationale"] == "Shorter range."

    def test_unknown_kind_rejected(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change(base_change(kind="frobnicate"), plan)
        assert cleaned is None
        assert any("kind" in e for e in errors)

    def test_missing_title_rejected(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change(base_change(title="  "), plan)
        assert cleaned is None
        assert any("title" in e for e in errors)

    def test_foreign_prescription_rejected(self):
        plan, _, _ = make_plan()
        other_presc = ExercisePrescriptionFactory()  # belongs to a different plan
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=other_presc.pk), plan
        )
        assert cleaned is None
        assert any("not in this plan" in e for e in errors)

    def test_off_week_target_rejected(self):
        # The agent is grounded on the current week only; an id from another
        # week of the same plan is out of contract.
        plan, session, _ = make_plan()  # week index 1 is current
        week2 = WeekFactory(mesocycle=session.week.mesocycle, index=2, is_current=False)
        off_session = SessionFactory(week=week2, day_number=1, name="Lower")
        off_presc = ExercisePrescriptionFactory(session=off_session, name="Squat")
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=off_presc.pk), plan
        )
        assert cleaned is None
        assert any("current week" in e for e in errors)

    def test_non_integer_prescription_id_rejected(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id="abc"), plan
        )
        assert cleaned is None
        assert any("integer" in e for e in errors)

    def test_deload_without_target_is_allowed(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change(base_change(kind="deload"), plan)
        assert errors == []
        assert cleaned["session"] is None
        assert cleaned["prescription"] is None

    def test_swap_without_a_target_is_rejected(self):
        plan, _, _ = make_plan()
        # The tool schema doesn't require a target id; an untargeted swap can't
        # be applied, so the guardrail drops it.
        raw = base_change()  # no prescription_id / session_id
        cleaned, errors = validation.clean_change(raw, plan)
        assert cleaned is None
        assert any("must target a prescription" in e for e in errors)

    def test_consistent_prescription_and_session_accepted(self):
        plan, session, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk, session_id=session.pk), plan
        )
        assert errors == []
        assert cleaned["prescription"] == presc
        assert cleaned["session"] == session

    def test_mismatched_prescription_and_session_rejected(self):
        plan, session, presc = make_plan()
        other_session = SessionFactory(week=session.week, day_number=2, name="Upper")
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk, session_id=other_session.pk), plan
        )
        assert cleaned is None
        assert any("not in the given session" in e for e in errors)

    def test_volume_change_targets_a_session(self):
        plan, session, _ = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(
                kind="volume",
                session_id=session.pk,
                introduces_exercise="",
                new_sets="4",
            ),
            plan,
        )
        assert errors == []
        assert cleaned["session"] == session

    def test_contraindication_backstop_rejects_flagged_swap(self):
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        plan, _, presc = make_plan(athlete=athlete)
        cleaned, errors = validation.clean_change(
            base_change(
                prescription_id=presc.pk,
                introduces_exercise="Deep Knee Flexion Drill",
            ),
            plan,
        )
        assert cleaned is None
        assert any("contraindication" in e for e in errors)

    def test_contraindication_backstop_checks_after_when_field_omitted(self):
        # The tool schema doesn't require introduces_exercise; a swap that omits
        # it must still be screened on the `after` text it would introduce.
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        plan, _, presc = make_plan(athlete=athlete)
        raw = base_change(prescription_id=presc.pk, after="Deep Knee Flexion Drill")
        raw.pop("introduces_exercise")
        cleaned, errors = validation.clean_change(raw, plan)
        assert cleaned is None
        assert any("contraindication" in e for e in errors)

    def test_contraindication_backstop_matches_plural_inflection(self):
        # 'avoid squats' must still catch a swap introducing a 'Goblet Squat'.
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="Patellar tendinopathy — avoid squats"
        )
        plan, _, presc = make_plan(athlete=athlete)
        cleaned, errors = validation.clean_change(
            base_change(
                prescription_id=presc.pk,
                introduces_exercise="Goblet Squat",
                after="Goblet Squat 3x10",
            ),
            plan,
        )
        assert cleaned is None
        assert any("contraindication" in e for e in errors)

    def test_contraindication_backstop_allows_safe_swap(self):
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        plan, _, presc = make_plan(athlete=athlete)
        cleaned, errors = validation.clean_change(
            base_change(
                prescription_id=presc.pk, introduces_exercise="Box Step-Down (low)"
            ),
            plan,
        )
        assert errors == []
        assert cleaned["introduces_exercise"] == "Box Step-Down (low)"

    def test_non_swap_change_mentioning_a_flagged_movement_is_allowed(self):
        # Only swaps introduce a movement; a volume/progress edit that merely
        # mentions a flagged movement (e.g. reducing it) is safe.
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="R shoulder — no overhead pressing"
        )
        plan, _, presc = make_plan(athlete=athlete)
        cleaned, errors = validation.clean_change(
            base_change(
                kind="volume",
                prescription_id=presc.pk,
                after="Overhead Pressing − 1 set",
                introduces_exercise="",
                new_sets="3",
            ),
            plan,
        )
        assert errors == []
        assert cleaned["kind"] == "volume"

    def test_field_lengths_are_truncated_and_types_coerced(self):
        plan, _, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk, honors="x" * 500, before=None),
            plan,
        )
        assert errors == []
        assert len(cleaned["honors"]) == 255
        assert cleaned["before"] == ""

    def test_non_dict_rejected(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change("not a dict", plan)
        assert cleaned is None
        assert errors


class TestApplyPayload:
    """The structured edit ``agent.apply`` performs is built here (Phase 2)."""

    def test_swap_payload_from_new_name(self):
        plan, _, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk, new_name="Box Squat"), plan
        )
        assert errors == []
        assert cleaned["payload"] == {"name": "Box Squat"}

    def test_swap_payload_falls_back_to_introduces_exercise(self):
        plan, _, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(prescription_id=presc.pk, introduces_exercise="Goblet Squat"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"name": "Goblet Squat"}

    def test_progress_payload_carries_load(self):
        plan, _, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="92.5 kg"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"load": "92.5 kg"}

    def test_volume_payload_carries_sets(self):
        plan, session, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="volume", session_id=session.pk, new_sets="4"), plan
        )
        assert errors == []
        assert cleaned["payload"] == {"sets": "4"}

    def test_deload_has_empty_payload(self):
        plan, _, _ = make_plan()
        cleaned, errors = validation.clean_change(base_change(kind="deload"), plan)
        assert errors == []
        assert cleaned["payload"] == {}

    def test_payload_values_are_length_capped(self):
        plan, _, presc = make_plan()
        cleaned, _ = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="x" * 99),
            plan,
        )
        assert len(cleaned["payload"]["load"]) == 32

    def test_progress_without_a_value_is_rejected(self):
        # A progress change with no new_load can't be applied; persisting it would
        # show an "approved" edit the apply step silently skips.
        plan, _, presc = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk), plan
        )
        assert cleaned is None
        assert any("value to apply" in e for e in errors)

    def test_volume_without_a_value_is_rejected(self):
        plan, session, _ = make_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="volume", session_id=session.pk), plan
        )
        assert cleaned is None
        assert any("value to apply" in e for e in errors)
