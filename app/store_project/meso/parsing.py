"""Tolerant, best-effort parser for freeform prescription cell text (Phase 2a).

The spreadsheet-parity model (docs/meso/spreadsheet-parity-plan.md §2.1) makes
a cell ONE freeform text string — ``4 x 6, RPE 9, 225`` / ``3 x 12-15`` /
``20-60m`` / ``AMRAP`` — and derives structure only when something needs it
(the agent, PR/tracking, analytics). This module is that derivation:

    parse_prescription(text) -> {sets?, reps?, reps_range?, rpe?, load?,
                                 unit?, duration?, amrap?, skip?, raw} | None

Slice 5a (docs/meso/parse-at-commit-plan.md §3) adds a second total function
for the *performed* side of the same cell:

    parse_performed(text) -> {kind, raw, ...} | None

``parse_performed`` classifies a cell into ``set`` / ``skip`` / ``swap`` /
``note`` / ``unresolved-set`` / ``duration`` — see its own docstring for the
load-first inversion that is the whole point of that function.

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


# ---------------------------------------------------------------------------
# parse_performed — the *performed* side (5a, parse-at-commit-plan.md §3)
# ---------------------------------------------------------------------------

# A bare, unit-less, non-decimal load over this many digits' worth of value
# is treated as an implausible fat-finger (e.g. ``2255`` from a doubled
# keystroke on ``225``) rather than a real single-exercise load, so it fails
# to resolve into a set — see ``_load_is_plausible``.
_MAX_BARE_LOAD = 999

# Keyword heuristic for the swap-vs-note split (§3): a swap is typically a
# bare exercise name (``DB pullover``, ``R SL L glute max``); a note reads
# like a sentence/comment (``felt tight``, ``paired with lat hang``). The
# plan gives examples, not a grammar, so this is a best-effort word list, not
# a formal rule.
_NOTE_SIGNAL_WORDS = {
    "felt",
    "feels",
    "feeling",
    "paired",
    "note",
    "notes",
    "comment",
    "comments",
    "with",
    "instead",
    "sore",
    "tired",
    "good",
    "bad",
    "easy",
    "hard",
    "tight",
    "rough",
    "great",
    "ok",
    "okay",
}


def _load_is_plausible(token):
    """Guard against a digit run masquerading as a load (``2255x5``).

    ``_LOAD`` is deliberately permissive (any digit run). A bare, unit-less,
    non-decimal number over ``_MAX_BARE_LOAD`` is almost certainly a
    mistyped duplicate keystroke rather than a real single-exercise load —
    percent/lbs/kg-suffixed and decimal loads are exempt (their notation
    already disambiguates intent), as is the ``bw`` literal.
    """
    match = _LOAD.match(token.strip())
    if not match:
        return False
    number, unit = match.groups()
    if number is None:  # the ``bw`` branch
        return True
    if unit or "." in number:
        return True
    return int(number) <= _MAX_BARE_LOAD


def _looks_like_set_attempt(segment):
    """Does ``segment`` have the *shape* of a logging attempt (§8)?

    Used only after every real parse route has failed — a digit alongside
    an ``x``/``×``/``@`` operator, or something that structurally matches
    ``_LOAD`` on its own (an implausible bare number), reads as a fat-finger
    rather than prose/a swap, so it warns instead of silently falling to
    ``swap``/``note``.
    """
    if not re.search(r"\d", segment):
        return False
    # ``x`` must be used as an OPERATOR, not just present as a letter. A bare
    # ``[x×@]`` search fires on ordinary exercise names that happen to contain
    # one — "Box squat 225", "Flexion 3" — and warns at the athlete for typing
    # a perfectly good swap. Require the operator to sit against a digit on at
    # least one side (``225 x``, ``x 5``, ``5 @ 225``), which is what an actual
    # fat-fingered set attempt looks like.
    if re.search(r"(\d\s*[x×@])|([x×@]\s*\d)", segment, re.IGNORECASE):
        return True
    return bool(_LOAD.match(segment.strip()))


def _looks_like_note(segment):
    """Best-effort note-vs-swap split — see ``_NOTE_SIGNAL_WORDS``."""
    words = re.findall(r"[a-z']+", segment.lower())
    return any(word in _NOTE_SIGNAL_WORDS for word in words)


def _try_at_form(head):
    """``5 @ 225`` — reps-at-load, the new operator for performed text."""
    parts = re.split(r"\s*@\s*", head, maxsplit=1)
    if len(parts) != 2:
        return None
    reps_token, load_token = parts[0].strip(), parts[1].strip()
    if not reps_token or not load_token:
        return None
    if not _LOAD.match(load_token) or not _load_is_plausible(load_token):
        return None
    out = {}
    _classify_reps(reps_token, out)
    if "reps" not in out and "reps_range" not in out:
        return None
    out["load"] = load_token.replace(" ", "")
    return out


def _try_load_first(head):
    """``225 x 5`` / ``30lbs x 8 each`` — load × reps, load claimed FIRST.

    This is the inversion vs. ``parse_prescription``: there, ``_SETS_X``
    claims a leading integer-then-``x`` as *sets* before this shape is ever
    tried. Here it is tried first, so ``225 x 5`` resolves to
    ``load="225", reps=5`` instead of ``sets=225``.
    """
    # IGNORECASE is load-bearing: the athlete types this on a phone, where the
    # keyboard auto-capitalizes, so ``225 X 5`` is everyday input. Without it
    # the split failed while `_looks_like_set_attempt` (which IS case-
    # insensitive) still recognized the `X` — so a perfectly good set was
    # tinted as a fat-finger and never logged.
    parts = re.split(r"\s*[x×]\s*", head, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return None
    if not _LOAD.match(left) or not _load_is_plausible(left):
        return None
    out = {"load": left.replace(" ", "")}
    _classify_reps(right, out)
    # The athlete wrote an ``x``, so they were attempting a set — if the
    # right-hand side yielded nothing recognizable (``225 x five``,
    # ``225x5x5``), this is a fat-fingered attempt, NOT a load-only partial.
    # Returning the load anyway would classify it ``set``, silently persist a
    # repless LoggedSet that can never count toward a record, and skip the
    # warning entirely — the athlete would think it logged. Bail so the
    # caller falls through to ``unresolved-set``. (A bare ``225`` with no
    # ``x`` is a different branch and still a legitimate partial set.)
    if not any(k in out for k in ("reps", "reps_range", "duration", "amrap")):
        return None
    return out


def _classify_performed_head(head):
    """Best-effort classification of the line's first (only) recognized set.

    Tries, in order: the ``@`` form, the load-first ``x`` form, then a bare
    load with no operator at all (``225`` — a partial set, load only). Returns
    ``None`` when none resolve, leaving the caller to decide between
    ``unresolved-set``/``swap``/``note``.
    """
    result = _try_at_form(head)
    if result is not None:
        return result
    result = _try_load_first(head)
    if result is not None:
        return result
    if _LOAD.match(head) and _load_is_plausible(head):
        return {"load": head.replace(" ", "")}
    return None


def parse_performed(text):
    """Derive best-effort structure from what an athlete typed into a cell.

    Total function — never raises, parity with ``parse_prescription``.
    Returns ``None`` for empty/whitespace text, else a dict always carrying
    ``raw`` (the stripped text) plus ``kind``, one of:

    - ``"set"`` — a recognized load/reps (``{load?, reps?, reps_range?,
      unit?, rpe?}``), a `LoggedSet` candidate.
    - ``"skip"`` — the freeform skip convention (``skip`` / ``-`` / ``—``).
    - ``"swap"`` — a substitute exercise name typed in place of a set.
    - ``"note"`` — prose/commentary, not a logging attempt.

      The ``swap``/``note`` split is a **best-effort keyword heuristic**
      (``_NOTE_SIGNAL_WORDS``), not a grammar — the plan gives examples only.
      In 5a the two are **behaviourally identical** (no set, no warn), so a
      misclassification is invisible. Do NOT make this distinction
      load-bearing without first replacing the word list with a real rule.

    - ``"unresolved-set"`` — text that has the *shape* of a set attempt
      (digit + ``x``/``×``/``@``) but doesn't resolve; the only kind that
      should warn (§8) — everything else is a successful classification.
    - ``"duration"`` — a bare timed cell (``30s``, ``20-60m``), not a lift.

    **Load-first is the whole point** (plan §3): unlike ``parse_prescription``
    (where a leading ``N x M`` is *sets*), here it's *load × reps* — reused
    verbatim are the ``_LOAD``/``_REPS``/``_RPE``/``_DURATION`` token regexes,
    plus the ``@`` operator (``5 @ 225``) and a swap/note branch for
    non-numeric text.

    **One set per line.** Only the line's first recognized set is returned;
    a later comma segment is only ever read for a trailing RPE (``225 x 5,
    RPE 8``). Multi-set-per-line text (``225x5, 230x3``) is explicitly OUT
    OF SCOPE — the second set is silently dropped, not an error.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    first_line = raw.splitlines()[0].strip()
    lowered = first_line.lower()

    if lowered in ("skip", "skipped", "-", "—"):
        return {"kind": "skip", "raw": raw}

    duration = _DURATION.match(first_line)
    if duration:
        return {
            "kind": "duration",
            "raw": raw,
            "duration": first_line.replace(" ", ""),
        }

    segments = first_line.split(",")
    head = segments[0].strip()
    out = _classify_performed_head(head)

    if out is not None:
        for segment in segments[1:]:
            segment = segment.strip().rstrip(".")
            rpe = _RPE.match(segment)
            if rpe:
                out.setdefault("rpe", re.sub(r"\s*", "", rpe.group(1)))
        out["kind"] = "set"
        out["raw"] = raw
        return out

    if _looks_like_set_attempt(first_line):
        return {"kind": "unresolved-set", "raw": raw, "warn": True}

    if _looks_like_note(first_line):
        return {"kind": "note", "raw": raw}

    return {"kind": "swap", "raw": raw}


def is_unresolved_set(text):
    """Derive-on-read ``warn`` (5a, plan §8): does ``text`` classify as ``unresolved-set``?

    A tiny wrapper around ``parse_performed`` shared by every surface that
    needs to color a cell — the athlete presenter (the sub-line stack) and the
    ``athlete_cell_write`` response (so a re-blur's answer updates live,
    without a full page reload). No stored flag, no new column: every call
    re-classifies ``text`` from scratch. Only the ``unresolved-set`` kind
    warns; ``set``/``skip``/``swap``/``note``/``duration`` (and an empty cell,
    which parses to ``None``) never do.
    """
    parsed = parse_performed(text)
    return bool(parsed) and parsed.get("kind") == "unresolved-set"


def performed_reps_text(parsed):
    """The reps a performed ``set`` should store, as ``LoggedSet.reps`` text.

    ``parse_performed`` splits the right-hand side of a set across four keys
    depending on what the athlete wrote — ``reps`` (``225 x 5``),
    ``reps_range`` (``225 x 5-8``), ``duration`` (``225 x 30s``) and ``amrap``
    (``225 x AMRAP``). Reading only ``reps`` drops the other three on the
    floor, writing a blank so the set renders as ``— @ 225`` in coach results
    and recent-log grounding even though the athlete recorded something
    perfectly meaningful.

    ``LoggedSet.reps`` is a free-text ``CharField`` (the structured logger
    stores plain strings too), so each form round-trips as the athlete's own
    notation. Returns ``""`` when nothing was recognized.
    """
    if not parsed:
        return ""
    # The suffix rides along: ``8 each`` and ``5 breaths`` mean something
    # different from a bare 8 or 5, and dropping it would show the set as plain
    # reps in coach results and recent-log grounding. (Time suffixes never
    # reach here — `_classify_reps` routes those to ``duration``.)
    unit = parsed.get("unit") or ""
    suffix = f" {unit}" if unit else ""
    if "reps" in parsed:
        return f"{parsed['reps']}{suffix}"
    if "reps_range" in parsed:
        low, high = parsed["reps_range"]
        return f"{low}-{high}{suffix}"
    if parsed.get("duration"):
        return str(parsed["duration"])
    if parsed.get("amrap"):
        return "AMRAP"
    return ""
