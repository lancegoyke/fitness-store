"""Golden eval cases for the Meso agent (Phase 4).

A small corpus of realistic coach instructions paired with *invariants* the
agent's output must uphold, so quality doesn't silently regress. The hard checks
are deliberately model-agnostic — a competent model has latitude in *what* it
proposes, but never in these:

- **responsive** — an actionable instruction yields at least one change;
- **grounded**   — every persisted change targets a real row in the plan;
- **safe**       — no persisted change re-introduces a contraindicated movement
                   (verifies the deterministic guardrail end-to-end).

Soft expectations (the *kind* of edit, an ``honors`` note on a swap) are reported
as warnings but don't fail a case, since a competent model may legitimately pick
a different safe edit.

Run against the real model with ``manage.py meso_agent_eval`` (needs
``ANTHROPIC_API_KEY``); ``--dry-run`` exercises the same harness with the
``ScriptedEvalClient`` below and no network. The CI tests in
``tests/test_agent_evals.py`` run the corpus through scripted clients so the
checks are covered without a key.
"""

from dataclasses import dataclass
from dataclasses import field

from ..models import AgentProposalBatch
from . import service
from . import validation


@dataclass(frozen=True)
class GoldenCase:
    name: str
    instruction: str
    # Hard: at least this many persisted changes (0 = the model may decline).
    min_changes: int = 1
    # Soft: at least one change should be one of these kinds.
    expect_kinds: frozenset = frozenset()
    # Soft: a swap should carry an ``honors`` note.
    require_honors_on_swaps: bool = False


GOLDEN_CASES = [
    GoldenCase(
        name="knee_safe_swap",
        instruction=(
            "Her left knee is bothering her — swap the back squat for a "
            "knee-friendly alternative."
        ),
        expect_kinds=frozenset({"swap"}),
        require_honors_on_swaps=True,
    ),
    GoldenCase(
        name="progress_main_lift",
        instruction="Progress the main lower-body lift by a small, defensible step.",
        expect_kinds=frozenset({"progress"}),
    ),
    GoldenCase(
        name="cut_volume",
        instruction="Lower the volume on the first lower day — she's run down.",
        expect_kinds=frozenset({"volume"}),
    ),
    GoldenCase(
        name="deload_week",
        instruction="Make this a deload week.",
        expect_kinds=frozenset({"deload"}),
    ),
]


@dataclass
class EvalResult:
    case: GoldenCase
    passed: bool
    failures: list = field(default_factory=list)  # hard invariant violations
    warnings: list = field(default_factory=list)  # soft expectation misses
    n_changes: int = 0
    n_rejected: int = 0
    summary: str = ""


def check_result(case, plan, batch, rejected):
    """Return ``(failures, warnings)`` for a resolved batch against a golden case.

    ``failures`` are hard invariant violations (a case with any fails); ``warnings``
    are soft expectation misses (reported, never fatal).
    """
    failures = []
    warnings = []
    changes = list(batch.changes.all())

    if len(changes) < case.min_changes:
        failures.append(
            f"expected at least {case.min_changes} change(s), got {len(changes)}"
        )

    forbidden = validation.forbidden_terms(plan)
    for ch in changes:
        # grounded — an actionable edit must point at a real target.
        if ch.kind in ("swap", "progress") and ch.prescription_id is None:
            failures.append(f"{ch.kind} change {ch.pk} has no prescription target")
        if ch.kind == "volume" and ch.session_id is None:
            failures.append(f"volume change {ch.pk} has no session target")
        # safe — nothing a contraindication forbids may be introduced by a swap.
        if ch.kind == "swap" and forbidden:
            hit = (
                validation.introduced_terms(
                    ch.payload.get("name", ""), ch.introduces_exercise, ch.after
                )
                & forbidden
            )
            if hit:
                failures.append(
                    f"swap {ch.pk} introduces forbidden movement {sorted(hit)}"
                )

    # Soft expectations.
    kinds = {ch.kind for ch in changes}
    if case.expect_kinds and not (kinds & case.expect_kinds):
        want = "/".join(sorted(case.expect_kinds))
        got = ", ".join(sorted(kinds)) or "none"
        warnings.append(f"expected a {want} edit; got {got}")
    if case.require_honors_on_swaps:
        for ch in changes:
            if ch.kind == "swap" and not ch.honors.strip():
                warnings.append(f"swap {ch.pk} has no honors note")

    return failures, warnings


def evaluate(plan, case, *, client):
    """Run one golden case against ``plan`` with ``client`` and check invariants."""
    batch, rejected = service.propose_changes(
        plan,
        case.instruction,
        coach=plan.coach,
        client=client,
        trigger=AgentProposalBatch.Trigger.EVAL,
    )
    failures, warnings = check_result(case, plan, batch, rejected)
    return EvalResult(
        case=case,
        passed=not failures,
        failures=failures,
        warnings=warnings,
        n_changes=batch.changes.count(),
        n_rejected=len(rejected),
        summary=batch.summary,
    )


def _first_targets(context):
    """First (session_id, prescription_id, prescription_name) in the plan context."""
    program = (context.get("plan") or {}).get("program") or []
    for session in program:
        exercises = session.get("exercises") or []
        if exercises:
            return session.get("id"), exercises[0].get("id"), exercises[0].get("name")
        if session.get("id") is not None:
            return session.get("id"), None, None
    return None, None, None


class ScriptedEvalClient:
    """A no-network client for ``--dry-run``: returns one safe, grounded edit.

    Reads the plan context to target a real row, so the eval harness runs
    end-to-end without a key. It is a wiring smoke-test, not a quality signal —
    it deterministically picks a safe edit matching the instruction's intent.
    """

    model = "scripted-eval"

    def propose(self, *, context, instruction):
        session_id, presc_id, presc_name = _first_targets(context)
        name = presc_name or "Back Squat"
        text = instruction.lower()

        if "swap" in text and presc_id is not None:
            return {
                "summary": "Scripted knee-safe swap.",
                "changes": [
                    {
                        "kind": "swap",
                        "prescription_id": presc_id,
                        "title": f"{name} → Hip Thrust",
                        "before": name,
                        "after": "Hip Thrust",
                        "rationale": "Scripted safe swap.",
                        "honors": "left knee",
                        "introduces_exercise": "Hip Thrust",
                        "new_name": "Hip Thrust",
                    }
                ],
            }
        if "deload" in text:
            return {
                "summary": "Scripted deload.",
                "changes": [
                    {
                        "kind": "deload",
                        "title": "Deload the current week",
                        "rationale": "Scripted deload.",
                    }
                ],
            }
        if "volume" in text and session_id is not None:
            return {
                "summary": "Scripted volume cut.",
                "changes": [
                    {
                        "kind": "volume",
                        "session_id": session_id,
                        "title": "Trim a set on day 1",
                        "rationale": "Scripted volume reduction.",
                        "new_sets": "3",
                    }
                ],
            }
        if presc_id is not None:
            return {
                "summary": "Scripted progression.",
                "changes": [
                    {
                        "kind": "progress",
                        "prescription_id": presc_id,
                        "title": f"{name} → +2.5 kg",
                        "rationale": "Scripted small progression.",
                        "new_load": "82.5 kg",
                    }
                ],
            }
        return {"summary": "No targets available to edit.", "changes": []}
