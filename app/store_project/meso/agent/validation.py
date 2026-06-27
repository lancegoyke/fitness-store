"""Deterministic guardrails for agent-proposed changes (B6).

Contraindications are enforced *here*, server-side, not just in the prompt: the
service runs every candidate the model returns through ``clean_change`` before
anything is persisted, so a hallucinated or unsafe edit never reaches the review
screen. Two layers:

1. **Structural** — the ``kind`` is valid, a title is present, and any referenced
   ``session``/``prescription`` resolves to a row within *this* plan (a foreign
   id is dropped, never applied).
2. **Contraindication backstop** — ``forbidden_terms`` extracts the actionable
   "avoid" phrase from each *active* contraindication; a swap whose
   ``introduces_exercise`` re-uses one of those movement terms is rejected. This
   is conservative and word-level by design — the prompt does the nuanced
   reasoning, this runs regardless of what the model returned.
"""

import re

from ..models import ExercisePrescription
from ..models import Session

VALID_KINDS = {"swap", "progress", "volume", "deload"}

# Generic words that survive the length filter but carry no movement meaning.
_STOPWORDS = {"under", "while", "without", "every", "other", "their", "during"}

# Display fields the review screen renders, with their model ``max_length``.
_TEXT_FIELDS = {
    "day_label": 128,
    "title": 255,
    "before": 255,
    "after": 255,
    "honors": 255,
    "introduces_exercise": 255,
}


def _significant_words(text):
    """Movement-meaningful words: alphabetic, length >= 5, not a stopword."""
    return {
        w
        for w in re.findall(r"[a-z]+", text.lower())
        if len(w) >= 5 and w not in _STOPWORDS
    }


def _avoid_clause(text):
    """The actionable phrase of a contraindication.

    Prefer the clause after an em/en/hyphen dash ("L knee — avoid ..."), then the
    text after an ``avoid`` / ``no`` marker; fall back to the whole string.
    """
    for sep in ("—", "–", " - "):
        if sep in text:
            text = text.split(sep, 1)[1]
            break
    lowered = text.lower()
    for marker in ("avoid", "no "):
        idx = lowered.find(marker)
        if idx != -1:
            return text[idx + len(marker) :]
    return text


def forbidden_terms(plan):
    """Movement terms a swap must not re-introduce, from active contraindications."""
    terms = set()
    for c in plan.athlete.contraindications.filter(active=True):
        terms |= _significant_words(_avoid_clause(c.text))
    return terms


def _name_words(name):
    return set(re.findall(r"[a-z]+", name.lower()))


def _resolve(model, value, plan, label, errors, **scope):
    """Look up ``value`` (an id) within the plan, recording an error if it fails."""
    if value in (None, ""):
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label}_id {value!r} is not an integer")
        return None
    obj = model.objects.filter(pk=pk, **scope).first()
    if obj is None:
        errors.append(f"{label} {pk} is not in this plan")
    return obj


def clean_change(raw, plan, *, forbidden=None):
    """Validate and normalize one raw change dict.

    Returns ``(cleaned, errors)``: ``cleaned`` is a dict of model field values
    when valid (else ``None``); ``errors`` is a list of human-readable reasons
    (empty when valid).
    """
    if not isinstance(raw, dict):
        return None, ["change is not an object"]

    errors = []

    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        errors.append(f"unknown kind {kind!r}")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("missing title")

    cleaned = {"kind": kind}
    for field, max_len in _TEXT_FIELDS.items():
        value = raw.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            errors.append(f"{field} must be a string")
            value = ""
        cleaned[field] = value.strip()[:max_len]

    # Structural: targets must belong to this plan.
    presc = _resolve(
        ExercisePrescription,
        raw.get("prescription_id"),
        plan,
        "prescription",
        errors,
        session__week__mesocycle__plan=plan,
    )
    session = _resolve(
        Session,
        raw.get("session_id"),
        plan,
        "session",
        errors,
        week__mesocycle__plan=plan,
    )
    # A prescription implies its session; backfill for display/apply.
    if presc is not None and session is None:
        session = presc.session
    cleaned["prescription"] = presc
    cleaned["session"] = session

    # Contraindication backstop on the INTRODUCED movement. Check both
    # ``introduces_exercise`` and ``after`` — a swap may omit the former, but
    # ``after`` still names the new exercise. We deliberately do NOT check
    # ``title``/``before``: those name the *removed* exercise, which is often the
    # contraindicated one being swapped out (checking them would reject the fix).
    if forbidden is None:
        forbidden = forbidden_terms(plan)
    if forbidden:
        introduced = f"{cleaned['introduces_exercise']} {cleaned['after']}"
        hit = _name_words(introduced) & forbidden
        if hit:
            errors.append(
                "introduced movement violates a contraindication "
                f"({', '.join(sorted(hit))})"
            )

    if errors:
        return None, errors
    return cleaned, []
