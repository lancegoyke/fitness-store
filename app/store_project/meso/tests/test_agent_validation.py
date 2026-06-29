"""Agent slice Phase 1 — the deterministic validation guardrail.

``agent.validation`` is the server-side backstop B6 calls for: contraindications
are enforced here, not just in the prompt. Two layers — structural (targets must
belong to the plan; valid kind; sane fields) and a contraindication backstop (a
swap may not re-introduce a flagged movement). The service runs every candidate
the model returns through this before persisting anything.
"""

import pytest

from store_project.meso.agent import client
from store_project.meso.agent import validation
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import LoadType
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

    def test_contraindication_backstop_screens_the_apply_value(self):
        # new_name is what apply writes to the prescription; a contraindicated
        # movement hidden there (with innocuous after/introduces_exercise) must
        # still be caught, not just the display fields.
        athlete = UserFactory()
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        plan, _, presc = make_plan(athlete=athlete)
        cleaned, errors = validation.clean_change(
            base_change(
                prescription_id=presc.pk,
                new_name="Deep Knee Flexion Drill",
                after="Box Step-Down",
                introduces_exercise="Box Step-Down",
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


def make_percent_plan():
    """A plan whose single prescription is prescribed as a %1RM (S2 Phase 2a)."""
    plan, session, presc = make_plan()
    presc.load = "75"
    presc.load_type = LoadType.PERCENT
    presc.save(update_fields=["load", "load_type"])
    return plan, session, presc


class TestPercentProgressBound:
    """A ``progress`` on a %1RM-typed lift moves a PERCENTAGE (S2 Phase 2a).

    The agent is type-agnostic (it treats ``load`` as an opaque string), so this
    deterministic backstop keeps a %1RM progression in a sane percent band —
    stopping the model from turning "75%" into an absolute "180" — and normalizes
    the stored value to a bare number so the ``%`` suffix isn't doubled. The
    absolute path is deliberately left unbounded.
    """

    def test_accepts_a_sane_percent(self):
        plan, _, presc = make_percent_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="82"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"load": "82"}

    def test_strips_a_percent_sign_and_stray_unit(self):
        # The model may echo the '%' (or wrongly append a unit); the stored load
        # is normalized to a bare percent so the designer's suffix isn't doubled.
        plan, _, presc = make_percent_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="82.5 %"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"load": "82.5"}

    def test_rejects_an_absolute_looking_load(self):
        # 180 is a plausible kg/lb load but an absurd %1RM — the bound catches a
        # model that ignored the type and progressed it like an absolute weight.
        plan, _, presc = make_percent_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="180"),
            plan,
        )
        assert cleaned is None
        assert any("out of range" in e for e in errors)

    def test_rejects_a_non_numeric_percent(self):
        plan, _, presc = make_percent_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="heavy"),
            plan,
        )
        assert cleaned is None
        assert any("number" in e for e in errors)

    def test_allows_legitimate_supramaximal_percent(self):
        # Eccentric/walkout work above 100% is real programming; the ceiling is
        # set so it passes while a clearly-absolute number does not.
        plan, _, presc = make_percent_plan()
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="105"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"load": "105"}

    def test_absolute_progress_is_not_bounded(self):
        # Regression guard: the bound is %1RM-only. An absolute lift can carry a
        # large numeric load (and its unit text) exactly as before Phase 2a.
        plan, _, presc = make_plan()  # default ABSOLUTE
        cleaned, errors = validation.clean_change(
            base_change(kind="progress", prescription_id=presc.pk, new_load="180 kg"),
            plan,
        )
        assert errors == []
        assert cleaned["payload"] == {"load": "180 kg"}


class TestPercentAwarePrompt:
    """The prompt is what teaches the type-agnostic model about %1RM (S2 Phase 2a).

    A ``load_type`` of ``pct`` means the load is a percent of 1RM; the system
    prompt and the ``new_load`` tool field must say so.
    """

    def test_system_prompt_explains_load_type(self):
        assert "load_type" in client.SYSTEM_PROMPT
        assert "1RM" in client.SYSTEM_PROMPT

    def test_new_load_tool_field_mentions_percent(self):
        props = client.PROPOSE_TOOL["input_schema"]["properties"]
        new_load = props["changes"]["items"]["properties"]["new_load"]
        assert "%" in new_load["description"] or "1RM" in new_load["description"]
