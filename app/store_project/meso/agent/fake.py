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

from ..parsing import parse_prescription
from .validation import _singular

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

    Reads the same contraindication texts the real client is grounded on — the
    plan's ``athlete.contraindications`` (``agent.service.build_context``) — and
    pulls out plain lowercase words. Deliberately loose (no stemming, no stopword
    list, a shorter minimum length than ``validation._significant_words``) since
    this is a *pre-filter* to keep the demo tasteful; the real guardrail
    (``validation.clean_change``) is still the enforced backstop.
    """
    athlete = context.get("athlete") or {}
    texts = list(athlete.get("contraindications") or [])
    words = set()
    for text in texts:
        cleaned = re.sub(r"[^a-z\s]", " ", text.lower())
        # Fold plurals with the guardrail's own ``_singular`` so "avoid squats"
        # collides with the "Box Squat" candidate here, exactly as it would in
        # ``validation`` — otherwise the fake proposes a swap the downstream
        # guardrail rejects, and the batch quietly loses its headline edit.
        words.update(_singular(w) for w in cleaned.split() if len(w) >= 4)
    return words


def _honors_note(context, instruction):
    """The rule the swap honors, straight from the plan's own grounding.

    A real contraindication text (the athlete's) — the same line the coach sees
    on the athlete card, so the review gate reads "this respects *her* flag",
    not boilerplate. Prefer the flag the coach's ``instruction`` is actually
    about (word overlap — "her knee is cranky" picks the knee flag over an
    unrelated first-in-list one); ties and no-overlap fall back to the first.
    Truncated to the model column (``validation._LIMITS``); a plan with no
    contraindications gets a generic coaching-rule note instead of an empty
    honors line.
    """
    athlete = context.get("athlete") or {}
    texts = list(athlete.get("contraindications") or [])
    if not texts:
        return "the plan's movement preferences"
    instruction_words = {
        w
        for w in re.sub(r"[^a-z\s]", " ", (instruction or "").lower()).split()
        if len(w) >= 4
    }

    def overlap(text):
        text_words = {
            w for w in re.sub(r"[^a-z\s]", " ", text.lower()).split() if len(w) >= 4
        }
        return len(text_words & instruction_words)

    return max(texts, key=overlap)[:255]


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
    current_words = {
        _singular(w)
        for w in re.sub(r"[^a-z\s]", " ", (current_name or "").lower()).split()
    }
    for candidate in _SWAP_ALTERNATIVES:
        candidate_words = {_singular(w) for w in candidate.lower().split()}
        if candidate_words & current_words:
            continue  # not a real swap — same movement family as the current name
        if candidate_words & forbidden_words:
            continue  # would reintroduce a flagged movement
        return candidate
    return _FALLBACK_SWAP


def _pick_trim_row(rows, swap_session, used_ids):
    """``(session, exercise, current_sets)`` for an honest one-set volume trim.

    Targets ONE row (``prescription_id``), never the whole day: a day-wide
    ``new_sets`` derived from one row would silently *increase* any row that
    trains fewer sets (``apply._apply_volume`` writes the same count to every
    row in a session). Wants a row not already edited by the swap/progress,
    with a parseable count of 2+ (text-first: sets come from parsing the
    freeform cell); rows outside the swap's day are preferred so the batch
    visibly spans the week.
    """
    ordered = [r for r in rows if r[0].get("id") != swap_session.get("id")]
    ordered += [r for r in rows if r[0].get("id") == swap_session.get("id")]
    for session, exercise in ordered:
        if exercise.get("id") in used_ids:
            continue
        parsed = parse_prescription(exercise.get("text") or "") or {}
        current = parsed.get("sets")
        if current is not None and current >= 2:
            return session, exercise, current
    return None


def _bump_load(current_load):
    """A small, defensible progression on ``current_load``, respecting its notation.

    ``current_load`` is the parsed load token off the row's freeform text
    (Phase 2a) — a ``NN%`` token is a percentage of 1RM, bumped by a couple of
    points and capped near 100 (suffix kept); a bare number is a plate-loadable
    weight, bumped by 2.5 (kept unitless, matching the cells' own notation).
    Falls back to a sane default when there's no numeric load to bump (e.g.
    "BW" or a load-less cell), so the demo never emits an unparsable load.
    """
    load = (current_load or "").strip()
    if load.endswith("%"):
        try:
            current = float(load.rstrip("%").strip())
        except ValueError:
            return "80%"
        bumped = min(current + 2, 100)
        num = str(int(bumped)) if bumped == int(bumped) else str(bumped)
        return f"{num}%"
    try:
        current = float(load)
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
                "honors": _honors_note(context, instruction),
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
            parsed = parse_prescription(progress_exercise.get("text") or "") or {}
            current_load = parsed.get("load") or ""
            new_load = _bump_load(current_load)
            changes.append(
                {
                    "kind": "progress",
                    "prescription_id": progress_exercise.get("id"),
                    "title": f"{progress_name} → {new_load}",
                    # before/after are display-only, but the review card renders
                    # its strikethrough → arrow row unconditionally — leaving
                    # them empty shows a dangling arrow on camera.
                    "before": current_load,
                    "after": new_load,
                    "rationale": (
                        "A small, defensible step up from loads already handled "
                        "comfortably the last couple of sessions."
                    ),
                    "new_load": new_load,
                }
            )

        # 3) A one-set volume trim on a third row, elsewhere in the week if possible.
        used_ids = {swap_exercise.get("id")}
        if progress_row is not None:
            used_ids.add(progress_row[1].get("id"))
        trim = _pick_trim_row(rows, swap_session, used_ids)
        if trim is not None:
            trim_session, trim_exercise, current_sets = trim
            trim_name = trim_exercise.get("name") or "Exercise"
            new_sets = current_sets - 1
            changes.append(
                {
                    "kind": "volume",
                    "prescription_id": trim_exercise.get("id"),
                    "day_label": trim_session.get("name") or "",
                    "title": f"{trim_name} → {new_sets} sets",
                    "before": f"{current_sets} sets",
                    "after": f"{new_sets} sets",
                    "rationale": (
                        "Fatigue has been creeping up, so pulling back one set "
                        "keeps the stimulus without digging the hole any deeper."
                    ),
                    "new_sets": str(new_sets),
                }
            )

        summary = (
            "Swapped in a joint-friendly alternative, nudged a load forward "
            "where it's been earned, and trimmed a set where fatigue is building."
        )
        return {"summary": summary, "changes": changes}
