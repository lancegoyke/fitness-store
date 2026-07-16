"""Tolerant, best-effort parser for freeform prescription cell text (Phase 2a).

The spreadsheet-parity model (docs/meso/spreadsheet-parity-plan.md §2.1) makes
a cell ONE freeform text string — ``4 x 6, RPE 9, 225`` / ``3 x 12-15`` /
``20-60m`` / ``AMRAP`` — and derives structure only when something needs it
(the agent, PR/tracking, analytics). This module is that derivation:

    parse_prescription(text) -> {sets?, reps?, reps_range?, rpe?, load?,
                                 unit?, duration?, amrap?, skip?, raw} | None

Contract (deliberately loose — the templates are heterogeneous):

- Never raises, never blocks entry: unparseable text still returns ``{"raw"}``
  so callers can fall back to displaying the verbatim cell.
- ``None`` only for empty/whitespace text (an empty cell parses to nothing).
- Partial by design: ``3 x ?`` yields ``sets`` and no ``reps``; a packed
  circuit cell (``A) EDT 1. RDL x 6 …``) may yield nothing but ``raw``.
- Values keep the coach's notation: ``rpe`` stays a string (``"6-7"`` is a
  real RPE), ``load`` keeps its suffix (``"225"``, ``"85%"``, ``"30lbs"``),
  ``duration`` keeps its unit token (``"45s"``, ``"1m"``, ``"20-60m"``).
- ``skip`` recovers §2.6's freeform skip convention (a cell that just says
  ``skip``); the structured ``skipped`` flag on the model is authoritative
  for the em-dash grid cell — this key only classifies typed text.

The parse is NEVER persisted as truth — the text is the source of truth;
parse lazily wherever structure is needed. Test corpus: the verbatim cells in
the plan §1 and ``docs/meso/fixtures/templates/`` (see ``test_parsing.py``).
"""

import re

# ``3 x 12``-style head: optional "up to" hedge, a sets count, an ``x``, and a
# freeform reps token classified separately below. Also matches ``3x12``.
_SETS_X = re.compile(r"^(?:up\s+to\s+)?(\d+)\s*[x×]\s*(.+)$", re.IGNORECASE)
# ``1x`` — a bare "do it once" cell (metabolic benchmarks).
_SETS_ONLY = re.compile(r"^(?:up\s+to\s+)?(\d+)\s*[x×]$", re.IGNORECASE)
# ``RPE 9`` / ``RPE 6-7`` / ``rpe9`` — as its own segment or trailing token.
_RPE = re.compile(r"^rpe\s*(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)$", re.IGNORECASE)
# A duration token: ``45s`` / ``1m`` / ``20-60m`` / ``20-75 min`` / ``15'``.
_DURATION = re.compile(
    r"^(\d+(?:\s*-\s*\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hours?|')$",
    re.IGNORECASE,
)
# A load token: bare number (``225``), percent (``85%``), or suffixed weight
# (``30lbs`` / ``102.5 kg`` / ``45 lb``). ``BW`` (bodyweight) also counts.
_LOAD = re.compile(r"^(\d+(?:\.\d+)?)\s*(%|lbs?|kgs?|kilos?)?$|^bw$", re.IGNORECASE)
# A reps token inside the ``x``: number, range, placeholder, unit suffix.
_REPS = re.compile(
    r"^(\d+)(?:\s*-\s*(\d+))?\s*([a-z']+(?:\s+[a-z]+)*)?$", re.IGNORECASE
)

# Unit-word normalization for reps suffixes: ``e``/``ea`` → ``each``.
_UNIT_ALIASES = {"e": "each", "ea": "each", "ea.": "each"}
# Reps suffixes that are actually time units → the token is a duration.
_TIME_UNITS = {
    "s",
    "sec",
    "secs",
    "second",
    "seconds",
    "m",
    "min",
    "mins",
    "minute",
    "minutes",
    "h",
    "hour",
    "hours",
    "'",
}


def _classify_reps(token, out):
    """Fold the ``x``'s right-hand token into ``out`` (reps/range/duration)."""
    token = token.strip()
    if token in ("?", "??"):
        return  # placeholder — sets known, reps deliberately open
    if token.lower() == "amrap":
        out["amrap"] = True
        return
    match = _REPS.match(token)
    if not match:
        return
    first, second, suffix = match.groups()
    suffix = (suffix or "").strip().lower()
    suffix = _UNIT_ALIASES.get(suffix, suffix)
    if suffix in _TIME_UNITS:
        # ``45s`` / ``1m`` / a ``30-60s`` range: a timed set, not a rep count.
        out["duration"] = token.replace(" ", "")
        return
    if second is not None:
        out["reps_range"] = (int(first), int(second))
    else:
        out["reps"] = int(first)
    if suffix:
        out["unit"] = suffix


def _classify_segment(segment, out):
    """Best-effort classification of one comma/``@``-separated segment."""
    segment = segment.strip().rstrip(".")
    if not segment:
        return
    if segment.lower() == "amrap":
        out["amrap"] = True
        return
    rpe = _RPE.match(segment)
    if rpe:
        out.setdefault("rpe", re.sub(r"\s*", "", rpe.group(1)))
        return
    duration = _DURATION.match(segment)
    if duration:
        out.setdefault("duration", segment.replace(" ", ""))
        return
    sets_only = _SETS_ONLY.match(segment)
    if sets_only:
        out.setdefault("sets", int(sets_only.group(1)))
        return
    sets_x = _SETS_X.match(segment)
    if sets_x:
        out.setdefault("sets", int(sets_x.group(1)))
        _classify_reps(sets_x.group(2), out)
        return
    load = _LOAD.match(segment)
    if load:
        out.setdefault("load", segment.replace(" ", ""))
        return
    # ``30lbs x 2 each`` — a logged-execution line, load first.
    parts = re.split(r"\s*[x×]\s*", segment, maxsplit=1)
    if len(parts) == 2 and _LOAD.match(parts[0].strip()):
        out.setdefault("load", parts[0].strip().replace(" ", ""))
        _classify_reps(parts[1], out)


def compose_prescription_text(sets="", reps="", rpe="", load="", load_pct=False):
    """Compose canonical cell text from structured parts — ``4 x 6, RPE 9, 225``.

    The inverse-ish of ``parse_prescription``, in Lance's own notation (plan
    §1). Used by the Phase 2a data migration (old structured columns → one
    text cell) and by agent applies that rewrite one component of a parsed
    cell. All args are taken verbatim as strings (``reps`` may already be
    ``"12-15"`` or ``"10 each"``); blanks are omitted. A lone ``sets`` or
    ``reps`` keeps the ``x`` with a ``?`` placeholder — the templates' own
    convention for "deliberately open" (``3 x ?``).
    """
    sets = str(sets or "").strip()
    reps = str(reps or "").strip()
    rpe = str(rpe or "").strip()
    load = str(load or "").strip()
    parts = []
    if sets and reps:
        parts.append(f"{sets} x {reps}")
    elif sets:
        parts.append(f"{sets} x ?")
    elif reps:
        parts.append(f"? x {reps}")
    if rpe:
        parts.append(rpe if rpe.lower().startswith("rpe") else f"RPE {rpe}")
    if load:
        if load_pct and not load.endswith("%"):
            load = f"{load}%"
        parts.append(load)
    return ", ".join(parts)


def parse_prescription(text):
    """Derive best-effort structure from one freeform cell's text.

    See the module docstring for the contract. Returns ``None`` for an
    empty cell, otherwise a dict always carrying ``raw`` (the stripped
    text) plus whichever of ``sets``/``reps``/``reps_range``/``rpe``/
    ``load``/``unit``/``duration``/``amrap``/``skip`` were recognized.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    out = {"raw": raw}
    lowered = raw.lower()
    if lowered in ("skip", "skipped", "-", "—"):
        out["skip"] = True
        return out
    # Only the first line of a multi-line cell is classified — later lines
    # are prose (notes, substitutions) that segment-splitting would garble.
    first_line = raw.splitlines()[0]
    for segment in re.split(r",|@", first_line):
        _classify_segment(segment, out)
    return out
