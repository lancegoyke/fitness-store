"""Orchestrate one agent run: ground → call Claude → validate → persist (B6).

Phase 4 splits the run so it can happen off the request thread:

- ``create_drafting_batch`` persists a ``drafting`` batch the endpoint returns
  immediately (the coach sees "drafting…" while the work runs in the background).
- ``run_proposal_job`` does the work for an existing batch — grounds, calls the
  client (the network boundary), validates every candidate, persists the clean
  ones, and flips the batch to ``pending`` (ready for review) or ``failed`` (with
  the reason recorded in ``error``). It never raises: a background thread has no
  caller to surface to, so a failure becomes a ``failed`` batch.

``propose_changes`` keeps the original synchronous contract (raise on failure,
no batch persisted) for callers/tests that want a blocking run. Rejected
candidates are returned (for logging), never persisted — the review screen only
ever sees safe, in-plan edits, and the human review gate is unchanged.
"""

import logging

from django.db import transaction

from .. import models
from .. import serializers
from . import client as client_module
from . import validation

logger = logging.getLogger(__name__)

# How many recent logged sessions feed the agent's grounding.
RECENT_LOG_LIMIT = 5


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
        # What the athlete actually logged recently (Phase 4 grounding).
        "recent_logs": serializers.serialize_recent_logs(plan, limit=RECENT_LOG_LIMIT),
    }


def create_drafting_batch(plan, instruction, *, coach):
    """Persist a ``drafting`` batch the endpoint returns before the job runs."""
    return models.AgentProposalBatch.objects.create(
        plan=plan,
        coach=coach,
        instruction=instruction,
        status=models.AgentProposalBatch.Status.DRAFTING,
    )


def _persist_result(batch, result, *, model):
    """Validate the model's candidates and persist the clean ones onto ``batch``.

    Flips the batch to ``pending`` in one transaction and returns the list of
    rejected candidates (``{"raw": ..., "errors": [...]}``) for logging.
    """
    if not isinstance(result, dict):
        result = {}
    raw_changes = result.get("changes") or []
    summary = result.get("summary") or ""

    forbidden = validation.forbidden_terms(batch.plan)
    rejected = []

    with transaction.atomic():
        batch.summary = summary
        batch.model = model
        order = 0
        for raw in raw_changes:
            cleaned, errors = validation.clean_change(
                raw, batch.plan, forbidden=forbidden
            )
            if cleaned is None:
                rejected.append({"raw": raw, "errors": errors})
                continue
            models.ProposedChange.objects.create(batch=batch, order=order, **cleaned)
            order += 1
        batch.status = models.AgentProposalBatch.Status.PENDING
        batch.error = ""
        batch.save(update_fields=["summary", "model", "status", "error"])

    if rejected:
        logger.info(
            "Meso agent batch %s dropped %s unsafe/invalid candidate(s).",
            batch.pk,
            len(rejected),
        )
    return rejected


def _fail(batch, message):
    """Mark a drafting batch as ``failed`` with the reason for the status poll."""
    batch.status = models.AgentProposalBatch.Status.FAILED
    batch.error = (message or "The agent run failed.")[:2000]
    batch.save(update_fields=["status", "error"])
    return batch, []


def run_proposal_job(batch_id, *, client=None):
    """Run the agent for an existing ``drafting`` batch (the background path).

    Never raises — flips the batch to ``pending`` or ``failed``. Returns
    ``(batch, rejected)``.
    """
    batch = models.AgentProposalBatch.objects.select_related(
        "plan", "plan__relationship", "plan__relationship__athlete"
    ).get(pk=batch_id)
    try:
        client = client or client_module.get_default_client()
        if client is None:
            return _fail(batch, "The Meso agent is not configured (no API key).")

        # Network call outside any DB transaction; wrap provider failures.
        try:
            result = client.propose(
                context=build_context(batch.plan), instruction=batch.instruction
            )
        except Exception as exc:  # external boundary
            return _fail(batch, f"The agent request failed: {exc}")

        rejected = _persist_result(batch, result, model=getattr(client, "model", ""))
        return batch, rejected
    except Exception:  # never leave a batch stuck drafting
        logger.exception("Meso agent job crashed for batch %s", batch_id)
        return _fail(batch, "The agent run failed unexpectedly.")


def propose_changes(plan, instruction, *, coach, client=None):
    """Run the agent synchronously and persist a reviewable batch.

    Blocking contract (kept for direct callers + tests): raises
    ``AgentNotConfigured`` when no client is available and ``AgentError`` on a
    provider failure — and persists no batch in either case. Returns
    ``(batch, rejected)`` on success.
    """
    client = client or client_module.get_default_client()
    if client is None:
        raise AgentNotConfigured("The Meso agent is not configured (no API key).")

    context = build_context(plan)
    # Network call happens outside the DB transaction. Wrap provider failures
    # (timeouts, API errors, a bad configured model) as AgentError so the
    # endpoint degrades to a 502 instead of an unhandled 500.
    try:
        result = client.propose(context=context, instruction=instruction)
    except AgentError:
        raise
    except Exception as exc:  # external boundary
        raise AgentError(f"The agent request failed: {exc}") from exc

    with transaction.atomic():
        batch = models.AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction=instruction,
            status=models.AgentProposalBatch.Status.PENDING,
        )
        rejected = _persist_result(batch, result, model=getattr(client, "model", ""))

    return batch, rejected
