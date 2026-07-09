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
import time

from django.db import transaction

from .. import models
from .. import serializers
from ..billing import access as billing_access
from ..billing import agent_costs
from . import client as client_module
from . import validation

logger = logging.getLogger(__name__)

# How many recent logged sessions feed the agent's grounding.
RECENT_LOG_LIMIT = 5

# The canned instruction behind "Draft with AI": a coach-readable ask (it shows as
# the coach's turn in the persisted chat thread) that also tells the agent to build
# the bare scaffold out — swap the placeholder rows and ``add`` accessory work for
# the athlete's goal. The result lands in the review gate like any other batch.
DRAFT_INSTRUCTION = (
    "Draft a complete first training week for this athlete from scratch. The plan "
    'currently holds only placeholder rows named "New exercise" — for each '
    "training day, swap the placeholder for a sensible primary lift and add the "
    "accessory exercises that round the day out for the athlete's goal. Give every "
    "exercise sensible sets, reps, and an RPE target; leave loads blank or "
    "conservative since no training maxes are known yet. Honor every active "
    "contraindication — never program a movement one flags."
)


class AgentError(Exception):
    """Base class for agent failures the endpoint surfaces to the coach."""


class AgentNotConfigured(AgentError):
    """No Claude client is configured (no API key) — surfaced as a 503."""


def _coach_style(coach):
    profile = getattr(coach, "coach_profile", None)
    if profile is None:
        return {"tags": [], "avoid": ""}
    return {"tags": profile.programming_style or [], "avoid": profile.avoid_rules}


def _group_context(group):
    """Group grounding (groups agent — the group agent edits a group's program).

    A group plan has no single athlete, so the agent grounds on the *group*: its
    name/focus, each active member (with a stable ``member_id`` the agent targets
    a per-athlete ``adjust`` by, Phase 2) and their own active contraindications,
    and — most importantly for a *shared* edit's safety — the **folded** set of
    every member's contraindications. A shared row trains everyone, so the agent
    must honor the union; the deterministic backstop (``validation.forbidden_terms``)
    folds the same way. A per-member adjust is screened against that one member's
    constraints instead (``validation.member_forbidden_terms``).
    """
    # One query with the contraindication prefetch (mirrors ``active_member_users``
    # scoping). The membership pk is the ``member_id`` an ``adjust`` change targets.
    memberships = (
        group.memberships.select_related("relationship", "relationship__athlete")
        .prefetch_related("relationship__athlete__contraindications")
        .filter(
            relationship__coach=group.coach,
            relationship__status=models.CoachAthlete.Status.ACTIVE,
        )
        .order_by("relationship__athlete__name", "relationship__athlete__email")
    )
    member_data = []
    folded = set()
    for membership in memberships:
        user = membership.relationship.athlete
        texts = [c.text for c in user.contraindications.all() if c.active]
        folded.update(texts)
        member_data.append(
            {
                "member_id": membership.pk,
                "name": user.display_name(),
                "contraindications": texts,
            }
        )
    return {
        "name": group.name,
        "focus": group.focus or "",
        "member_count": len(member_data),
        "members": member_data,
        # The union the shared program must honor — every member's constraints.
        "contraindications": sorted(folded),
    }


def build_context(plan):
    """Everything the model is grounded on, as a JSON-serializable dict.

    An **individual** plan grounds on its one athlete (profile, contraindications,
    recent logs). A **group** plan grounds on the group instead (members + the
    contraindications folded across them); there is no single athlete log stream,
    so ``recent_logs`` is empty for a group.

    Both paths carry the whole current **block** (``serialize_agent_block``): every
    live week of the plan's current mesocycle with its full session/cell grid and
    per-week volume/intensity/phase/current flags, so the agent programs
    progression across the block, not one week in isolation (P4).
    """
    context = {
        "plan": serializers.serialize_plan(plan),
        "coach_style": _coach_style(plan.coach),
        "block": serializers.serialize_agent_block(plan),
    }
    if plan.is_group:
        context["group"] = _group_context(plan.group)
        context["recent_logs"] = []
        return context
    context["athlete"] = {
        "name": plan.athlete.display_name(),
        "contraindications": [
            c.text for c in plan.athlete.contraindications.filter(active=True)
        ],
    }
    # What the athlete actually logged recently (Phase 4 grounding).
    context["recent_logs"] = serializers.serialize_recent_logs(
        plan, limit=RECENT_LOG_LIMIT
    )
    return context


def create_drafting_batch(
    plan, instruction, *, coach, trigger=models.AgentProposalBatch.Trigger.MANUAL
):
    """Persist a ``drafting`` batch the endpoint returns before the job runs.

    Snapshots the slicing dimensions for the usage ledger at creation: ``trigger``
    (what kicked the run off) and the coach's ``billing_status`` *now* (lossy to
    reconstruct later — COGS vs CAC). The usage/cost columns fill in once the job
    resolves the batch (``_persist_result`` / ``_fail``).
    """
    return models.AgentProposalBatch.objects.create(
        plan=plan,
        coach=coach,
        instruction=instruction,
        status=models.AgentProposalBatch.Status.DRAFTING,
        trigger=trigger,
        billing_status=billing_access.billing_status(coach),
    )


def _apply_usage(batch, usage, *, model, duration_ms):
    """Stamp the captured usage + estimated cost onto ``batch`` (no save).

    Returns the list of ``update_fields`` written, so the caller saves exactly the
    touched columns. The cost is computed at write time from the per-model rate
    table (``None`` for an unknown model — we don't guess).
    """
    batch.input_tokens = usage.input_tokens
    batch.output_tokens = usage.output_tokens
    batch.cache_creation_input_tokens = usage.cache_creation_input_tokens
    batch.cache_read_input_tokens = usage.cache_read_input_tokens
    batch.api_calls = usage.api_calls
    batch.request_id = (usage.request_id or "")[:128]
    batch.stop_reason = (usage.stop_reason or "")[:32]
    batch.duration_ms = duration_ms
    batch.estimated_cost_usd = agent_costs.estimate_cost(model, usage)
    return [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "api_calls",
        "request_id",
        "stop_reason",
        "duration_ms",
        "estimated_cost_usd",
    ]


def _persist_result(batch, result, *, model, duration_ms=None):
    """Validate the model's candidates and persist the clean ones onto ``batch``.

    Flips the batch to ``pending`` in one transaction — writing the run's token
    usage + estimated cost alongside (the batch is the per-run usage ledger) — and
    returns the list of rejected candidates (``{"raw": ..., "errors": [...]}``) for
    logging. ``result`` may be a ``client.ProposalResult`` (the real client, with
    usage) or a bare dict (scripted/test clients → zero usage); both normalize.
    """
    normalized = client_module.normalize_result(result)
    data = normalized.data
    raw_changes = data.get("changes") or []
    summary = data.get("summary") or ""

    forbidden = validation.forbidden_terms(batch.plan)
    rejected = []

    with transaction.atomic():
        batch.summary = summary
        batch.model = model
        usage_fields = _apply_usage(
            batch, normalized.usage, model=model, duration_ms=duration_ms
        )
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
        batch.save(update_fields=["summary", "model", "status", "error", *usage_fields])

    if rejected:
        logger.info(
            "Meso agent batch %s dropped %s unsafe/invalid candidate(s).",
            batch.pk,
            len(rejected),
        )
    return rejected


def _fail(batch, message, *, model="", duration_ms=None):
    """Mark a drafting batch ``failed`` with the reason for the status poll.

    Records ``model`` + ``duration_ms`` when known so a failed run still attributes
    in the usage report (U5). Token usage stays zero: a non-streaming call that
    raised before returning gave us no ``usage`` block to capture — the Anthropic
    invoice reconciliation (deferred) covers any tokens billed on such a drop.
    """
    batch.status = models.AgentProposalBatch.Status.FAILED
    batch.error = (message or "The agent run failed.")[:2000]
    fields = ["status", "error"]
    if model:
        batch.model = model
        fields.append("model")
    if duration_ms is not None:
        batch.duration_ms = duration_ms
        fields.append("duration_ms")
    batch.save(update_fields=fields)
    return batch, []


def run_proposal_job(batch_id, *, client=None):
    """Run the agent for an existing ``drafting`` batch (the background path).

    Never raises — flips the batch to ``pending`` or ``failed``. Returns
    ``(batch, rejected)``.
    """
    batch = models.AgentProposalBatch.objects.select_related(
        "plan", "plan__relationship", "plan__relationship__athlete", "plan__group"
    ).get(pk=batch_id)
    try:
        client = client or client_module.get_default_client()
        if client is None:
            return _fail(batch, "The Meso agent is not configured (no API key).")

        model = getattr(client, "model", "")
        # Network call outside any DB transaction; wrap provider failures. Time it
        # for the usage ledger — recorded on both the success and the failure path.
        started = time.monotonic()
        try:
            result = client.propose(
                context=build_context(batch.plan), instruction=batch.instruction
            )
        except Exception as exc:  # external boundary
            duration_ms = int((time.monotonic() - started) * 1000)
            return _fail(
                batch,
                f"The agent request failed: {exc}",
                model=model,
                duration_ms=duration_ms,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        rejected = _persist_result(batch, result, model=model, duration_ms=duration_ms)
        return batch, rejected
    except Exception:  # never leave a batch stuck drafting
        logger.exception("Meso agent job crashed for batch %s", batch_id)
        return _fail(batch, "The agent run failed unexpectedly.")


def propose_changes(
    plan,
    instruction,
    *,
    coach,
    client=None,
    trigger=models.AgentProposalBatch.Trigger.MANUAL,
):
    """Run the agent synchronously and persist a reviewable batch.

    Blocking contract (kept for direct callers + tests): raises
    ``AgentNotConfigured`` when no client is available and ``AgentError`` on a
    provider failure — and persists no batch in either case. Returns
    ``(batch, rejected)`` on success. ``trigger`` tags the run for the usage ledger
    (the eval harness passes ``eval`` so its runs are excluded from cost reports).
    """
    client = client or client_module.get_default_client()
    if client is None:
        raise AgentNotConfigured("The Meso agent is not configured (no API key).")

    context = build_context(plan)
    model = getattr(client, "model", "")
    # Network call happens outside the DB transaction. Wrap provider failures
    # (timeouts, API errors, a bad configured model) as AgentError so the
    # endpoint degrades to a 502 instead of an unhandled 500.
    started = time.monotonic()
    try:
        result = client.propose(context=context, instruction=instruction)
    except AgentError:
        raise
    except Exception as exc:  # external boundary
        raise AgentError(f"The agent request failed: {exc}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    with transaction.atomic():
        batch = models.AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction=instruction,
            status=models.AgentProposalBatch.Status.PENDING,
            trigger=trigger,
            billing_status=billing_access.billing_status(coach),
        )
        rejected = _persist_result(batch, result, model=model, duration_ms=duration_ms)

    return batch, rejected
