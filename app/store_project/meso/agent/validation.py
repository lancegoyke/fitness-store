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
from ..serializers import current_week

VALID_KINDS = {"swap", "progress", "volume", "deload"}

# What an actionable change of each kind must resolve to within the plan. A swap
# or progression edits a specific exercise row; a volume change edits a day; a
# deload is week/plan-level and needs no specific row. (A resolved prescription
# backfills its session, so a "session" requirement is met by either.)
_REQUIRED_TARGET = {
    "swap": "prescription",
    "progress": "prescription",
    "volume": "session",
}

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

# The structured edit ``agent.apply`` performs per kind, as
# (prescription/week field, the tool field that supplies it, model ``max_length``).
# A deload has no value — it flags the week — so it is absent here.
_APPLY_FIELD = {
    "swap": ("name", "new_name", 255),
    "progress": ("load", "new_load", 32),
    "volume": ("sets", "new_sets", 32),
}


def _singular(word):
    """Cheap plural fold so 'squats' matches 'squat'. Keeps 'ss' (e.g. 'press').

    Not a full stemmer — it folds the common plural -s only; richer inflection
    (e.g. -ing) is left to the later guardrail/eval hardening phase.
    """
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _significant_words(text):
    """Movement-meaningful words: alphabetic, length >= 5, not a stopword."""
    return {
        _singular(w)
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
    # Singular-folded so a plural contraindication term matches a singular name.
    return {_singular(w) for w in re.findall(r"[a-z]+", name.lower())}


def introduced_terms(*texts):
    """Significant, singular-folded movement words across name/text fragments.

    The public form of the swap-introduces tokenizer — used by the eval harness
    to check, end-to-end, that no persisted change re-introduces a forbidden term.
    """
    terms = set()
    for text in texts:
        terms |= _name_words(text or "")
    return terms


def _resolve(model, value, label, errors, **scope):
    """Look up ``value`` (an id) within ``scope``, recording an error if it fails."""
    if value in (None, ""):
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label}_id {value!r} is not an integer")
        return None
    obj = model.objects.filter(pk=pk, **scope).first()
    if obj is None:
        errors.append(f"{label} {pk} is not in this plan's current week")
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

    # ``rationale`` is the model's explanation the review screen shows; it's a
    # TextField (no length cap), so it's copied separately from the CharFields.
    rationale = raw.get("rationale", "")
    cleaned["rationale"] = rationale.strip() if isinstance(rationale, str) else ""

    # Structural: targets must belong to the plan's CURRENT week — the agent is
    # grounded on (and only edits) that week, so an id from another week is out
    # of contract even if it belongs to the same plan.
    week = current_week(plan)
    presc = _resolve(
        ExercisePrescription,
        raw.get("prescription_id"),
        "prescription",
        errors,
        session__week=week,
    )
    session = _resolve(
        Session,
        raw.get("session_id"),
        "session",
        errors,
        week=week,
    )
    # A prescription's own session is authoritative: backfill it when no session
    # was given, and reject a session_id that points at a different day (the
    # model supplied contradictory targets).
    if presc is not None:
        if session is not None and session.pk != presc.session_id:
            errors.append("prescription is not in the given session")
        session = presc.session
    cleaned["prescription"] = presc
    cleaned["session"] = session

    # An actionable change must target a real row (a swap/progress with no
    # prescription, or a volume change with no session, can't be applied).
    required = _REQUIRED_TARGET.get(kind)
    if required == "prescription" and presc is None:
        errors.append(f"a {kind} change must target a prescription")
    elif required == "session" and session is None:
        errors.append(f"a {kind} change must target a session")

    # The structured edit the apply step (Phase 2) performs, built before the
    # contraindication backstop so the swap's *apply value* is screened too. A
    # swap falls back to the introduced exercise when the model omits an explicit
    # new name, so a Phase-1-shaped swap still applies.
    payload = {}
    spec = _APPLY_FIELD.get(kind)
    if spec is not None:
        field, raw_field, max_len = spec
        value = raw.get(raw_field, "")
        value = value.strip()[:max_len] if isinstance(value, str) else ""
        if not value and kind == "swap":
            value = cleaned["introduces_exercise"]
        if value:
            payload[field] = value
    cleaned["payload"] = payload

    # Contraindication backstop — only a SWAP introduces a new movement, so only
    # swaps are screened (a volume/progress edit that *mentions* a flagged
    # movement, e.g. "overhead pressing − 1 set", is safe and must pass). Check
    # the name actually applied (``payload['name']``, which folds in ``new_name``)
    # plus ``introduces_exercise`` and ``after`` — any of them can carry the new
    # exercise. We deliberately do NOT check ``title``/``before``: those name the
    # *removed* exercise, often the contraindicated one being swapped out.
    if forbidden is None:
        forbidden = forbidden_terms(plan)
    if forbidden and kind == "swap":
        introduced = (
            f"{payload.get('name', '')} "
            f"{cleaned['introduces_exercise']} {cleaned['after']}"
        )
        hit = _name_words(introduced) & forbidden
        if hit:
            errors.append(
                "introduced movement violates a contraindication "
                f"({', '.join(sorted(hit))})"
            )

    # Progress/volume can only be applied with a concrete value, so an empty
    # payload means the change can't be applied — drop it rather than persist an
    # "approved" edit the apply step would silently skip. A swap is exempt: it
    # falls back to its (contraindication-checked) introduced exercise.
    if kind in ("progress", "volume") and not payload:
        errors.append(f"a {kind} change needs a value to apply ({spec[1]})")

    if errors:
        return None, errors
    return cleaned, []
