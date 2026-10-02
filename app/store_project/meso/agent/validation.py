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

from ..models import Prescription
from ..models import ProposedChange
from ..models import Session

VALID_KINDS = {"swap", "progress", "volume", "deload", "add"}

# A %1RM progression moves a PERCENT, so its value is bounded to a sane band. The
# ceiling sits above legitimate supramaximal work (eccentrics / walkouts run a
# little over 100%) but well below a number that is plainly an absolute load the
# type-agnostic model mistyped (e.g. "180"). The floor is just above zero.
MAX_PERCENT_1RM = 120

# What an actionable change of each kind must resolve to within the plan. A swap
# or progression edits a specific exercise row; a volume change or an add edits a
# day (volume rewrites its rows, add appends one); a deload is week/plan-level and
# needs no specific row. (A resolved prescription backfills its session, so a
# "session" requirement is met by either.)
_REQUIRED_TARGET = {
    "swap": "prescription",
    "progress": "prescription",
    "volume": "session",
    "add": "session",
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

# An ``add`` builds a whole new prescription, so it carries several fields rather
# than the single value the other kinds set, as (component, tool field, length
# cap). ``name`` is required (it falls back to introduces_exercise, like a
# swap); the rest compose into the new row's freeform cell text (Phase 2a).
_ADD_FIELDS = (
    ("name", "new_name", 255),
    ("sets", "new_sets", 32),
    ("reps", "new_reps", 32),
    ("load", "new_load", 32),
    ("rpe", "new_rpe", 32),
)


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


def _terms_from_texts(texts):
    """Fold a list of contraindication texts into their forbidden movement terms."""
    terms = set()
    for text in texts:
        terms |= _significant_words(_avoid_clause(text))
    return terms


def forbidden_terms(plan):
    """Movement terms a swap must not re-introduce, from active contraindications."""
    return _terms_from_texts(
        c.text for c in plan.athlete.contraindications.filter(active=True)
    )


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
        errors.append(f"{label} {pk} is not in this plan's current block")
    return obj


def _percent_load(text):
    """A %1RM progression's value as a float, or ``None`` if it isn't a percent.

    A %1RM load is a *bare* percentage — a number with an optional ``%`` sign
    ("82", "82.5 %"). A unit-suffixed or otherwise non-numeric string ("82.5 kg",
    "100 lb", "heavy") is the model converting the lift to an absolute weight,
    which is NOT a percent and must be rejected rather than silently reinterpreted
    (storing "100 lb" as "100%" would corrupt the prescribed intensity).
    """
    cleaned = (text or "").strip()
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1].strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    return float(cleaned)


def _fmt_percent(value):
    """A bare percent string ('82' / '82.5'); no '%' so the suffix isn't doubled."""
    return str(int(value)) if value == int(value) else str(value)


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _fill_display(cleaned):
    """Fill the review card's day tag and before → after from the program itself.

    ``before``/``after``/``day_label`` are display text the model is *asked* for
    but is free to leave blank (UAT round 2, #647: a deload-by-progress came
    back with both empty, so the card showed a bare "→" over a pre-approved
    Apply). The program already knows the answer — the target cell's current
    text, and what ``agent.apply`` would recompose it to — so a blank is derived
    here, at proposal time, and a model-supplied value is left as the model
    wrote it. A change whose edit can't be shown at all (neither side) starts
    ``rejected``: the coach must opt in to something they can't see.
    """
    from . import apply as agent_apply

    kind = cleaned["kind"]
    presc = cleaned["prescription"]
    session = cleaned["session"]
    payload = cleaned["payload"]

    if not cleaned["day_label"] and session is not None:
        cleaned["day_label"] = str(session)[:128]

    before = after = ""
    authoritative_after = None
    # Can ``apply_change`` actually write this? A progress/volume whose target
    # cell the parser can't read is a safe skip at apply time, so approving it
    # would be a no-op the card dressed up as an edit.
    applicable = True
    if kind == "swap" and presc is not None:
        before = presc.name
        after = payload.get("name") or cleaned["introduces_exercise"]
    elif kind in ("progress", "volume") and presc is not None:
        component = "load" if kind == "progress" else "sets"
        new_text = agent_apply.recomposed_text(presc, component, payload.get(component))
        if new_text is not None:
            before = _first_line(presc.text)
            # What the card shows is what the apply writes: derived here from
            # the same function, never the model's own rendering of the edit
            # (605.9c — it wrote ``3x5 @ 230`` on the card, ``3 x 5, 230`` landed).
            after = authoritative_after = _first_line(new_text)
            if len(after) > 255:
                # The card column can't hold the whole edit, so it can't show
                # byte-for-byte what lands: show nothing (never a truncation) and
                # start it Rejected.
                after = authoritative_after = ""
                applicable = False
        else:
            applicable = False
    elif kind == "volume" and session is not None and payload.get("sets"):
        if any(agent_apply._parsed_bits(cell) for cell in session.cells()):
            after = authoritative_after = f"{payload['sets']} sets on every exercise"
        else:
            applicable = False
    elif kind == "deload":
        before, after = "Training week", "Deload week"
    elif kind == "add":
        after = payload.get("name", "")

    if not cleaned["before"]:
        cleaned["before"] = before[:255]
    if authoritative_after is not None:
        cleaned["after"] = authoritative_after[:255]
    elif not cleaned["after"]:
        cleaned["after"] = after[:255]
    if not applicable or not (cleaned["before"] or cleaned["after"]):
        cleaned["status"] = ProposedChange.Status.REJECTED


def clean_change(raw, plan, *, mesocycle, forbidden=None):
    """Validate and normalize one raw change dict.

    ``mesocycle`` is the block this run is scoped to — the coach's *viewed*
    block, captured on the batch at request time (§4b,
    docs/meso/remove-current-week-plan.md). It is passed in, never re-derived:
    this function used to call ``current_week(plan)`` itself, which — once
    ``is_current`` was removed — would silently resolve to the plan's
    earliest-live week's block instead of the one the coach actually reviewed,
    dropping (or worse, misattributing) otherwise-valid edits. ``None`` is a
    legitimate value (no block, or one hard-deleted after the run started) and
    simply matches nothing, same as any other foreign scope.

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

    # Structural: targets must belong to the batch's block (``mesocycle``, the
    # coach's viewed block, §4b), in any LIVE week (P4 block-wide semantics).
    # The agent is grounded on that one block (``serialize_agent_block``), so a
    # structural swap renames the block-shared slot and numeric edits address
    # any *week* of that block — but a target from another block of the same
    # plan (a past/future mesocycle the coach did not review) is out of
    # contract and is dropped, just like a foreign-plan id.
    presc = _resolve(
        Prescription,
        raw.get("prescription_id"),
        "prescription",
        errors,
        exercise_slot__session_slot__mesocycle=mesocycle,
        exercise_slot__deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )
    session = _resolve(
        Session,
        raw.get("session_id"),
        "session",
        errors,
        session_slot__mesocycle=mesocycle,
        deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )
    # A prescription's own session is authoritative: backfill it when no session
    # was given, and reject a session_id that points at a different day (the
    # model supplied contradictory targets). A cell has no ``session`` FK of its
    # own (it's an ExerciseSlot × Week join) — its session is derived from the
    # slot's day within the cell's own week.
    if presc is not None:
        presc_session = Session.objects.filter(
            week_id=presc.week_id,
            session_slot_id=presc.exercise_slot.session_slot_id,
        ).first()
        if (
            session is not None
            and presc_session is not None
            and session.pk != presc_session.pk
        ):
            errors.append("prescription is not in the given session")
        session = presc_session
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
    elif kind == "add":
        # Build the new row from its fields; an absent name falls back to the
        # contraindication-checked introduces_exercise (same as a swap).
        for field, raw_field, max_len in _ADD_FIELDS:
            value = raw.get(raw_field, "")
            value = value.strip()[:max_len] if isinstance(value, str) else ""
            if value:
                payload[field] = value
        if not payload.get("name") and cleaned["introduces_exercise"]:
            payload["name"] = cleaned["introduces_exercise"]
    cleaned["payload"] = payload

    # %1RM bound — a progress on a lift currently prescribed as a percent moves
    # a PERCENTAGE. Text-first (Phase 2a): "percent-typed" is derived from the
    # target cell's parsed text (its load token ends in ``%``), not a stored
    # enum. Requires a clean percent in a sane band (rejecting both an
    # absolute-looking "180" and a unit-suffixed "100 lb" the model wrongly
    # converted) and normalizes a valid one to ``NN%`` so the recomposed cell
    # text keeps its percent notation. An absolute lift is left unbounded as
    # before.
    load_value = payload.get("load")
    if kind == "progress" and presc is not None and load_value:
        parsed = presc.parsed() or {}
        current_load = parsed.get("load") or ""
        if current_load.endswith("%"):
            pct = _percent_load(load_value)
            if pct is None:
                errors.append(
                    f"a %1RM progression must be a bare percent (got {load_value!r})"
                )
            elif not 0 < pct <= MAX_PERCENT_1RM:
                errors.append(
                    f"%1RM progression {pct:g}% is out of range "
                    f"(expected 1–{MAX_PERCENT_1RM}%)"
                )
            else:
                payload["load"] = f"{_fmt_percent(pct)}%"

    # Contraindication backstop — only a movement-introducing change is screened
    # (a volume/progress edit that *mentions* a flagged movement, e.g. "overhead
    # pressing − 1 set", is safe and must pass). We check the name actually
    # applied plus ``introduces_exercise``/``after`` — any can carry the new
    # exercise — but never ``title``/``before``, which name the *removed*
    # exercise.
    if forbidden is None:
        forbidden = forbidden_terms(plan)
    if forbidden and kind in ("swap", "add"):
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

    # An add must name the exercise it introduces (the new row needs a name), or
    # the apply step has nothing to create.
    if kind == "add" and not payload.get("name"):
        errors.append("an add change needs an exercise name (new_name)")

    if errors:
        return None, errors
    _fill_display(cleaned)
    return cleaned, []
