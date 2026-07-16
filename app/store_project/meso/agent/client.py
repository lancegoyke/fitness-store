"""The only network boundary of the Meso agent: the Claude (anthropic) call.

The service depends on a small interface — an object with a ``model`` attribute
and ``propose(*, context, instruction) -> dict`` — so tests inject a fake and
never touch the network. ``get_default_client`` builds the real client from
settings and returns ``None`` when no API key is configured, which the endpoint
surfaces as a 503 (the feature degrades cleanly without credentials).

Model + thinking (pinned against the ``claude-api`` reference at build time):
``claude-opus-4-8`` (settings ``MESO_AGENT_MODEL``). We force ``tool_choice`` to
the proposal tool so the model always returns a structured batch; **adaptive
thinking is omitted** because a forced ``tool_choice`` is incompatible with
extended/adaptive thinking, and this is a single constrained extraction. The
stable system prompt is sent with ``cache_control`` (prompt caching); the
volatile per-plan grounding + instruction go in the user turn. Revisit auto
``tool_choice`` + adaptive thinking when the agent becomes multi-turn — see
``docs/archive/meso/agent-plan.md``.
"""

import json
from dataclasses import dataclass
from dataclasses import field

from django.conf import settings

TOOL_NAME = "propose_program_changes"


@dataclass
class RunUsage:
    """Token usage + tracing metadata from one Claude proposal call (agent-usage v1).

    Captured at the call site (``MesoAgentClient.propose``) and threaded onto the
    ``AgentProposalBatch`` so a run's cost is attributable to the coach + athlete.
    Defaults are zero/empty so a no-network client (the scripted eval client, the
    test fakes that return a bare dict) records an honest "no API usage" run.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    request_id: str = ""
    stop_reason: str = ""
    # Completed Claude calls behind this run. Defaults to 0 (no network) so a
    # scripted/test client doesn't overcount the ledger; ``_extract_usage`` sets it
    # to 1 for a real response. >1 once group/multi-turn lands.
    api_calls: int = 0


@dataclass
class ProposalResult:
    """What ``propose`` returns: the tool data + the run's usage/tracing block.

    ``data`` is the tool-input dict (``summary`` + ``changes``) callers consume;
    ``usage`` is the captured token block. A client that returns a bare dict (the
    scripted eval client, test fakes) is coerced to one of these — with empty
    usage — by ``normalize_result``, so the service handles both uniformly.
    """

    data: dict = field(default_factory=dict)
    usage: RunUsage = field(default_factory=RunUsage)


def normalize_result(result):
    """Coerce a client's ``propose`` return into a ``ProposalResult``.

    Accepts a ``ProposalResult`` (the real client), a bare dict (fakes / scripted
    clients that don't measure usage → empty usage), or anything else (→ empty).
    """
    if isinstance(result, ProposalResult):
        return result
    if isinstance(result, dict):
        return ProposalResult(data=result)
    return ProposalResult()


PROPOSE_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Propose a batch of edits to the athlete's whole training BLOCK. The plan "
        "context gives every week's rows and numbers; target a real session or "
        "exercise by the id given there. A 'swap' changes the exercise for the "
        "WHOLE block (every week follows), so it targets any week's "
        "prescription_id. A 'progress', 'volume', or 'deload' acts on the specific "
        "week's row/day you target by id. An 'add' introduces a NEW exercise row "
        "into a session, so it targets a session_id, not a prescription. For a "
        "GROUP's shared program you have an extra verb: 'adjust' diverges ONE "
        "member from the shared row (a per-athlete auto-adjust) — it targets that "
        "member by member_id plus the shared prescription_id, and the other "
        "members are unaffected. Honor every active contraindication and the "
        "coach's avoid-rules — never introduce a movement a contraindication flags."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences summarizing the batch for the coach."
                ),
            },
            "changes": {
                "type": "array",
                "description": "The proposed edits (may be empty).",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "swap",
                                "progress",
                                "volume",
                                "deload",
                                "add",
                                "adjust",
                            ],
                        },
                        "session_id": {
                            "type": ["integer", "null"],
                            "description": (
                                "Target session id from the plan context — may be "
                                "any week's day (required for an 'add', which "
                                "creates a row in that session)."
                            ),
                        },
                        "prescription_id": {
                            "type": ["integer", "null"],
                            "description": (
                                "Target exercise-row cell id from the plan context "
                                "(any week). A swap renames that exercise for the "
                                "WHOLE block; a progress sets that specific week's "
                                "load."
                            ),
                        },
                        "day_label": {
                            "type": "string",
                            "description": "e.g. 'Day 1 · Lower'.",
                        },
                        "title": {"type": "string"},
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "rationale": {"type": "string"},
                        "honors": {
                            "type": "string",
                            "description": (
                                "The contraindication or coaching rule this respects."
                            ),
                        },
                        "introduces_exercise": {
                            "type": "string",
                            "description": (
                                "For a swap, the exercise being introduced (checked "
                                "by the contraindication guardrail); else empty."
                            ),
                        },
                        "new_name": {
                            "type": "string",
                            "description": (
                                "swap/add: the exercise name to set on the row "
                                "(defaults to introduces_exercise if omitted). A "
                                "swap renames the exercise for the whole block — "
                                "every week follows."
                            ),
                        },
                        "new_load": {
                            "type": "string",
                            "description": (
                                "progress (or an optional starting load for add): "
                                "the load to set, matching the row's own notation. "
                                "If the row's text prescribes an absolute load, a "
                                "bare number in the plan's unit, e.g. '92.5'; if "
                                "it prescribes a %1RM (its load ends in '%'), a "
                                "percentage like '82%' — never convert between "
                                "the two."
                            ),
                        },
                        "new_sets": {
                            "type": "string",
                            "description": (
                                "volume/add: the set count to set, e.g. '4'."
                            ),
                        },
                        "new_reps": {
                            "type": "string",
                            "description": (
                                "add only: the rep target for the new row, e.g. '8-10'."
                            ),
                        },
                        "new_rpe": {
                            "type": "string",
                            "description": (
                                "add only: the RPE target for the new row, e.g. '7'."
                            ),
                        },
                        "member_id": {
                            "type": ["integer", "null"],
                            "description": (
                                "adjust only (GROUP plans): the member_id (from the "
                                "group context) of the ONE member this per-athlete "
                                "auto-adjust applies to. The shared row is unchanged; "
                                "only this member diverges."
                            ),
                        },
                        "load_pct": {
                            "type": ["integer", "null"],
                            "description": (
                                "adjust only: scale this member's shared load to "
                                "this percentage — 90 means −10%, 110 means +10%. "
                                "Omit for no load change (set new_name for a swap "
                                "and/or new_sets/new_reps for a volume tweak instead)."
                            ),
                        },
                    },
                    "required": ["kind", "title", "rationale"],
                },
            },
        },
        "required": ["summary", "changes"],
    },
}

SYSTEM_PROMPT = (
    "You are an expert strength & conditioning programming assistant working "
    "inside a coach's program designer. A coach gives you an instruction and the "
    "current state of one athlete's training plan. The plan context includes the "
    "athlete's FULL training block — every week's session rows, each carrying a "
    "freeform prescription 'text' cell in the coach's own notation (e.g. "
    "'4 x 6, RPE 9, 225' / '3 x 12-15' / 'AMRAP'), optional freeform sub-'lines' "
    "(an RPE row, cues, logged deviations), per-exercise tempo/rest/instructions, "
    "plus each week's volume/intensity/phase and which week is current. Propose "
    "concrete edits, programming progression ACROSS the weeks of the block.\n\n"
    "Rules:\n"
    "- Emit edits ONLY through the propose_program_changes tool.\n"
    "- Target real rows by the ids in the plan context. Do not invent ids. Each "
    "row/day id belongs to a specific week — a progress, volume, or deload acts "
    "on the week you target by id, so address each week's rows by that week's "
    "ids.\n"
    "- A swap changes the exercise for the WHOLE block: renaming a row updates "
    "every week's copy of it, so swap once, not per week.\n"
    "- Honor every active contraindication and the coach's avoid-rules. If a "
    "contraindication flags a movement, do not introduce it — choose a safe "
    "alternative and name it in introduces_exercise.\n"
    "- Anchor load progressions to the values already in the plan; prefer small, "
    "defensible steps from one week to the next.\n"
    "- Use the 'add' kind to introduce a NEW exercise into a day: target the day "
    "by session_id, give the exercise in new_name, and set new_sets/new_reps/"
    "new_rpe (and new_load only if you want a starting weight). This is how you "
    "build out a bare or placeholder plan — fill each day with the work the "
    "athlete's goal calls for, and swap any placeholder rows for real lifts. For "
    "an add, set 'after' to the new exercise and leave 'before' empty so the "
    "review reads cleanly.\n"
    "- Read each row's load from its text cell: a load ending in '%' is a "
    "percentage of 1RM — progress it as a percentage (keep it in a sane range, "
    "typically at or below 100%) — never convert it into an absolute weight, "
    "and never convert an absolute lift into a percentage.\n"
    "- Give the value to apply: new_name for a swap, new_load for a progress, "
    "new_sets for a volume change. A deload needs no value.\n"
    "- Set 'honors' to the specific contraindication or coaching rule each change "
    "respects.\n"
    "- If the instruction needs no change, return an empty changes array and say "
    "so in the summary."
)


_GROUP_FRAMING = (
    "This is a GROUP's SHARED program — every listed member trains off it. You "
    "can edit it two ways:\n"
    "- A SHARED edit (swap/progress/volume/deload/add) changes the shared program "
    "for EVERYONE — a swap changes that exercise across the WHOLE block, and "
    "progress/volume/deload act on the week you target by id. Honor every member's "
    "contraindications (group.contraindications folds them together) — a shared "
    "movement must be safe for all of them.\n"
    "- A per-athlete ADJUST diverges ONE member from the shared row (a swap, a "
    "load %, or a volume tweak just for them). Use it when the instruction is "
    "about a single athlete or when one member's constraint differs from the "
    "group. Set member_id to that member's id from group.members, prescription_id "
    "to the shared row, and the divergence (new_name / load_pct / new_sets / "
    "new_reps). An adjust only needs to be safe for THAT member.\n"
    "Prefer a shared edit when the change is for the whole group; reach for an "
    "adjust only to personalize one member.\n\n"
)


def _user_prompt(context, instruction):
    # Group framing lives in the volatile user turn (not the cached system prompt)
    # so individual runs keep their stable, cache-friendly prompt unchanged.
    framing = _GROUP_FRAMING if "group" in context else ""
    return (
        f"{framing}"
        "Plan context (JSON):\n"
        f"{json.dumps(context, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Coach instruction:\n{instruction}"
    )


def _extract_usage(message):
    """Build a ``RunUsage`` from an anthropic ``Message`` (defensive getattrs).

    The cache token fields are absent on a response that used no prompt caching, so
    each read tolerates a missing attribute and a ``None`` value → 0. ``_request_id``
    is the SDK's per-call id (private attr, but the documented accessor).
    """
    raw = getattr(message, "usage", None)
    return RunUsage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        request_id=getattr(message, "_request_id", "") or "",
        stop_reason=getattr(message, "stop_reason", "") or "",
        api_calls=1,
    )


class MesoAgentClient:
    """Wraps the anthropic SDK (lazily imported so it isn't a hard dependency)."""

    def __init__(self, *, api_key, model):
        self.api_key = api_key
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def propose(self, *, context, instruction):
        """Call Claude with a forced proposal tool; return a ``ProposalResult``.

        The tool-input dict (``data``) plus the call's token usage + ``_request_id``
        + ``stop_reason`` (``usage``). The usage feeds the per-run cost ledger
        (``AgentProposalBatch``) so a run is attributable to the coach + athlete;
        the Anthropic invoice stays the billing source of truth, our number is an
        internal estimate.
        """
        message = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[PROPOSE_TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": _user_prompt(context, instruction)}],
        )
        data = {"summary": "", "changes": []}
        for block in message.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                data = dict(block.input)
                break
        return ProposalResult(data=data, usage=_extract_usage(message))


def get_default_client():
    """The configured client, or ``None`` when no API key is set.

    Checked before the API-key gate: demo/sandbox mode (``MESO_AGENT_FAKE``,
    #388/#389) swaps in a curated, no-network ``FakeDemoClient`` so a recorded
    walkthrough or a public sandbox needs no real key at all — and never spends
    one, even if ``ANTHROPIC_API_KEY`` happens to be configured.
    """
    if getattr(settings, "MESO_AGENT_FAKE", False):
        from .fake import FakeDemoClient

        return FakeDemoClient()
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    model = getattr(settings, "MESO_AGENT_MODEL", "claude-opus-4-8")
    return MesoAgentClient(api_key=api_key, model=model)
