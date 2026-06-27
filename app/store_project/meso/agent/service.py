"""Orchestrate one agent run: ground → call Claude → validate → persist (B6).

``propose_changes`` is the entry point the endpoint calls. It builds the grounding
context from the plan, calls the client (the network boundary), validates every
candidate it returns, and persists a batch holding only the clean ones. Rejected
candidates are returned (for logging), never persisted — so the review screen only
ever sees safe, in-plan edits. The human review gate is unchanged.
"""

from django.db import transaction

from .. import models
from .. import serializers
from . import client as client_module
from . import validation


class AgentError(Exception):
    """Base class for agent failures the endpoint surfaces to the coach."""


class AgentNotConfigured(AgentError):
    """No Claude client is configured (no API key) — surfaced as a 503."""


def _coach_style(coach):
    profile = getattr(coach, "coach_profile", None)
    if profile is None:
        return {"tags": [], "avoid": ""}
    return {"tags": profile.programming_style or [], "avoid": profile.avoid_rules}


def build_context(plan):
    """Everything the model is grounded on, as a JSON-serializable dict."""
    return {
        "plan": serializers.serialize_plan(plan),
        "athlete": {
            "name": plan.athlete.display_name(),
            "contraindications": [
                c.text for c in plan.athlete.contraindications.filter(active=True)
            ],
        },
        "coach_style": _coach_style(plan.coach),
    }


def propose_changes(plan, instruction, *, coach, client=None):
    """Run the agent for ``plan`` and persist a reviewable batch.

    Returns ``(batch, rejected)`` — ``rejected`` is a list of
    ``{"raw": ..., "errors": [...]}`` for candidates the guardrail dropped.
    Raises ``AgentNotConfigured`` when no client is available.
    """
    client = client or client_module.get_default_client()
    if client is None:
        raise AgentNotConfigured("The Meso agent is not configured (no API key).")

    context = build_context(plan)
    # Network call happens outside the DB transaction.
    result = client.propose(context=context, instruction=instruction)
    if not isinstance(result, dict):
        result = {}
    raw_changes = result.get("changes") or []
    summary = result.get("summary") or ""

    forbidden = validation.forbidden_terms(plan)
    rejected = []

    with transaction.atomic():
        batch = models.AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction=instruction,
            summary=summary,
            model=getattr(client, "model", ""),
        )
        order = 0
        for raw in raw_changes:
            cleaned, errors = validation.clean_change(raw, plan, forbidden=forbidden)
            if cleaned is None:
                rejected.append({"raw": raw, "errors": errors})
                continue
            models.ProposedChange.objects.create(batch=batch, order=order, **cleaned)
            order += 1

    return batch, rejected
