"""Demo/sandbox mode: a pre-baked agent client with no Anthropic call (#388/#389).

``FakeDemoClient`` stands in for ``MesoAgentClient`` when ``MESO_AGENT_FAKE`` is
set — for a re-recordable walkthrough video or a public sandbox, where a curated,
repeatable proposal reads better on camera than whatever a live model happens to
return, and where no real API key should be required (or spent) at all.

Like ``evals.ScriptedEvalClient``, it reads the plan context
(``agent.service.build_context``) to target real rows so the downstream
guardrail (``agent.validation.clean_change``) accepts the batch end-to-end; unlike
the eval client it aims for polish — a small, curated batch with coach-voiced
copy, since this is exactly what a prospective coach sees in the review gate on
camera. No randomness, no network, no ``anthropic`` import.
"""

import re

from ..models import LoadType

# Candidate swap targets, roughly ordered by how often they're a safe alternative
# to a common lower-body/posterior-chain lift. Picked by elimination against the
# plan's contraindication words, so the demo never has to rely on the
# (real, still-enforced) validation guardrail rejecting an unsafe suggestion.
_SWAP_ALTERNATIVES = [
    "Box Squat",
    "Hip Thrust",
    "Trap Bar Deadlift",
    "Chest-Supported Row",
    "Goblet Squat",
    "Supported Split Squat",
]

# A generic, low-risk fallback if every candidate above collides with the plan's
# contraindications (an unlikely, but not impossible, demo plan).
_FALLBACK_SWAP = "Coach's Choice Alternative"


def _contraindication_words(context):
    """Movement words the demo must not reintroduce.

    Reads the same contraindication texts the real client is grounded on — an
    individual plan's ``athlete.contraindications`` or a group plan's folded
    ``group.contraindications`` (``agent.service.build_context``) — and pulls out
    plain lowercase words. Deliberately loose (no stemming, no stopword list, a
    shorter minimum length than ``validation._significant_words``) since this is a
    *pre-filter* to keep the demo tasteful; the real guardrail
    (``validation.clean_change``) is still the enforced backstop.
    """
    athlete = context.get("athlete") or {}
    group = context.get("group") or {}
    texts = list(athlete.get("contraindications") or [])
    texts += list(group.get("contraindications") or [])
    words = set()
    for text in texts:
        cleaned = re.sub(r"[^a-z\s]", " ", text.lower())
        words.update(w for w in cleaned.split() if len(w) >= 4)
    return words


def _honors_note(context):
    """The rule the swap honors, straight from the plan's own grounding.

    The first contraindication text (athlete's, else the group's folded list) —
    the same line the coach sees on the athlete card, so the review gate reads
    "this respects *her* flag", not boilerplate. Truncated to the model column
    (``validation._LIMITS``); a plan with no contraindications gets a generic
    coaching-rule note instead of an empty honors line.
    """
    athlete = context.get("athlete") or {}
    group = context.get("group") or {}
    texts = list(athlete.get("contraindications") or [])
    texts += list(group.get("contraindications") or [])
    if texts:
        return texts[0][:255]
    return "the plan's movement preferences"


def _pick_swap_row(rows):
    """The row a joint-sparing swap tells the best story on.

    Prefer the first row *not* already tagged as a curated-safe pick (e.g.
    ``knee-safe``): swapping the one exercise the coach already made safe
    undercuts the honors line right next to it. Falls back to the first row.
    """
    for session, exercise in rows:
        tag = (exercise.get("tag") or "").lower()
        if "safe" not in tag:
            return session, exercise
    return rows[0]


def _pick_swap_name(current_name, forbidden_words):
    """The first candidate alternative that doesn't collide with ``forbidden_words``."""
    current_words = set(re.sub(r"[^a-z\s]", " ", (current_name or "").lower()).split())
    for candidate in _SWAP_ALTERNATIVES:
        candidate_words = set(candidate.lower().split())
        if candidate_words & current_words:
            continue  # not a real swap — same movement family as the current name
        if candidate_words & forbidden_words:
            continue  # would reintroduce a flagged movement
        return candidate
    return _FALLBACK_SWAP


def _bump_load(load_type, current_load):
    """A small, defensible progression on ``current_load``, respecting its type.

    A ``pct`` row is a bare percentage of 1RM — bumped by a couple of points and
    capped near 100; an ``abs`` row is a plate-loadable weight — bumped by 2.5.
    Both stay **bare numbers**: ``apply`` writes ``new_load`` verbatim into the
    prescription's ``load`` column, and every existing row stores loads unitless
    (the unit lives on the plan) — a suffixed "62.5 kg" would render as the one
    inconsistent cell in the designer grid. Falls back to a sane default when the
    current value isn't a plain number (e.g. "BW"), so the demo never emits an
    unparsable load.
    """
    if load_type == LoadType.PERCENT:
        try:
            current = float((current_load or "").rstrip("%").strip())
        except ValueError:
            return "80"
        bumped = min(current + 2, 100)
        return str(int(bumped)) if bumped == int(bumped) else str(bumped)
    try:
        current = float((current_load or "").strip())
    except ValueError:
        return "62.5"
    bumped = current + 2.5
    return str(int(bumped)) if bumped == int(bumped) else str(bumped)


class FakeDemoClient:
    """No-network stand-in for ``MesoAgentClient`` (demo/sandbox mode)."""

    model = "meso-fake-demo"

    def propose(self, *, context, instruction):
        program = (context.get("plan") or {}).get("program") or []
        rows = [
            (session, exercise)
            for session in program
            for exercise in (session.get("exercises") or [])
        ]

        if not rows:
            return {
                "summary": (
                    "This week doesn't have any exercises yet — build out the "
                    "days and I'll take it from there."
                ),
                "changes": [],
            }

        forbidden = _contraindication_words(context)
        changes = []

        # 1) A joint-friendly swap — on the row that isn't already the safe pick.
        swap_session, swap_exercise = _pick_swap_row(rows)
        current_name = swap_exercise.get("name") or "Exercise"
        swap_name = _pick_swap_name(current_name, forbidden)
        changes.append(
            {
                "kind": "swap",
                "prescription_id": swap_exercise.get("id"),
                "day_label": swap_session.get("name") or "",
                "title": f"{current_name} → {swap_name}",
                "before": current_name,
                "after": swap_name,
                "rationale": (
                    f"{swap_name} trains the same pattern through a shorter, more "
                    "forgiving range of motion — the work stays hard without "
                    "aggravating anything."
                ),
                "honors": _honors_note(context),
                "introduces_exercise": swap_name,
                "new_name": swap_name,
            }
        )

        # 2) A small, load-type-aware progression on a different row, if there is one.
        progress_row = next(
            ((s, e) for s, e in rows if e.get("id") != swap_exercise.get("id")),
            None,
        )
        if progress_row is not None:
            _, progress_exercise = progress_row
            progress_name = progress_exercise.get("name") or "Exercise"
            load_type = progress_exercise.get("load_type") or LoadType.ABSOLUTE
            new_load = _bump_load(load_type, progress_exercise.get("load"))
            suffix = "%" if load_type == LoadType.PERCENT else ""
            changes.append(
                {
                    "kind": "progress",
                    "prescription_id": progress_exercise.get("id"),
                    "title": f"{progress_name} → {new_load}{suffix}",
                    "rationale": (
                        "A small, defensible step up from loads already handled "
                        "comfortably the last couple of sessions."
                    ),
                    "new_load": new_load,
                }
            )

        # 3) A volume tweak on a different session, if the plan has more than one.
        volume_session = next(
            (s for s in program if s.get("id") != swap_session.get("id")), None
        )
        if volume_session is not None:
            changes.append(
                {
                    "kind": "volume",
                    "session_id": volume_session.get("id"),
                    "title": f"{volume_session.get('name') or 'This day'} → trim a set",
                    "rationale": (
                        "Fatigue has been creeping up, so pulling back one set "
                        "keeps the stimulus without digging the hole any deeper."
                    ),
                    "new_sets": "3",
                }
            )

        summary = (
            "Swapped in a joint-friendly alternative, nudged a load forward "
            "where it's been earned, and trimmed a set where fatigue is building."
        )
        return {"summary": summary, "changes": changes}
