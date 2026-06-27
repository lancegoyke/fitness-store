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
``docs/meso/agent-plan.md``.
"""

import json

from django.conf import settings

TOOL_NAME = "propose_program_changes"

PROPOSE_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Propose a batch of edits to the athlete's CURRENT training week. Each "
        "change must target a real session or exercise by the id given in the "
        "plan context. Honor every active contraindication and the coach's "
        "avoid-rules — never introduce a movement a contraindication flags."
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
                            "enum": ["swap", "progress", "volume", "deload"],
                        },
                        "session_id": {
                            "type": ["integer", "null"],
                            "description": "Target session id from the plan context.",
                        },
                        "prescription_id": {
                            "type": ["integer", "null"],
                            "description": (
                                "Target exercise row id from the plan context "
                                "(required for swap/progress)."
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
                                "swap only: the exercise name to set on the row "
                                "(defaults to introduces_exercise if omitted)."
                            ),
                        },
                        "new_load": {
                            "type": "string",
                            "description": (
                                "progress only: the load to set, e.g. '92.5 kg'."
                            ),
                        },
                        "new_sets": {
                            "type": "string",
                            "description": "volume only: the set count to set, e.g. '4'.",
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
    "current state of one athlete's training plan. Propose concrete edits to the "
    "athlete's CURRENT week.\n\n"
    "Rules:\n"
    "- Emit edits ONLY through the propose_program_changes tool.\n"
    "- Target real rows by the ids in the plan context. Do not invent ids.\n"
    "- Honor every active contraindication and the coach's avoid-rules. If a "
    "contraindication flags a movement, do not introduce it — choose a safe "
    "alternative and name it in introduces_exercise.\n"
    "- Anchor load progressions to the values already in the plan; prefer small, "
    "defensible steps.\n"
    "- Give the value to apply: new_name for a swap, new_load for a progress, "
    "new_sets for a volume change. A deload needs no value.\n"
    "- Set 'honors' to the specific contraindication or coaching rule each change "
    "respects.\n"
    "- If the instruction needs no change, return an empty changes array and say "
    "so in the summary."
)


def _user_prompt(context, instruction):
    return (
        "Plan context (JSON):\n"
        f"{json.dumps(context, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Coach instruction:\n{instruction}"
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
        """Call Claude with a forced proposal tool; return the tool input dict."""
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
        for block in message.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                return dict(block.input)
        return {"summary": "", "changes": []}


def get_default_client():
    """The configured client, or ``None`` when no API key is set."""
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    model = getattr(settings, "MESO_AGENT_MODEL", "claude-opus-4-8")
    return MesoAgentClient(api_key=api_key, model=model)
