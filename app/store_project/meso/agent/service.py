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


def build_context(plan, mesocycle):
    """Everything the model is grounded on, as a JSON-serializable dict.

    Grounds on the plan's one athlete (profile, contraindications, recent logs)
    plus the whole ``mesocycle`` **block** (``serialize_agent_block``): every
    live week of it with its full session/cell grid and per-week
    volume/intensity/phase/deload flags, so the agent programs progression
    across the block, not one week in isolation (P4).

    ``mesocycle`` is the coach's *viewed* block — resolved once, at request
    time, by whoever created the batch (``AgentProposalBatch.mesocycle``, §4b)
    — and threaded straight through, never re-derived here. May be ``None``
    (no block to program, or one hard-deleted mid-run); ``serialize_agent_block``
    degrades to an empty block rather than silently falling back to a different
    one.
    """
    # Both halves of the context must describe the SAME block. ``serialize_plan``
    # resolves its own opening week via ``current_week(plan)`` — the plan's
    # earliest live week — so left alone it would expose block 1's prescription
    # ids beside block 2's ``block`` payload. The model can only see ids, not
    # which block they belong to, so it would happily target the block-1 ones and
    # validation would then drop them as outside ``batch.mesocycle``. Pin it to
    # this block's first live week.
    #
    # A resolved block can still have no live week of its own (``mesocycle`` is
    # ``None``, or every week in it was soft-deleted — reachable since
    # ``week_delete`` only guards the *plan's* last live week, not the block's,
    # docs/meso/remove-current-week-plan.md §4b). ``week=None`` is
    # ``serialize_plan``'s "no override" sentinel, so passing it through
    # unconditionally would make it fall back to ``current_week(plan)`` — the
    # plan's earliest live week, quite possibly sitting in a DIFFERENT block —
    # and hand the model that other block's prescription ids again, right back
    # in the failure mode this function exists to close. So: when this block has
    # no live week, force the block-scoped rows ``serialize_plan`` would itself
    # produce for "no open week" (``program``/``weeks`` empty, ``viewing`` null)
    # rather than let its fallback reach past this (empty) block into another.
    resolved_week = serializers.first_live_week(mesocycle)
    plan_context = serializers.serialize_plan(plan, week=resolved_week)
    if resolved_week is None:
        plan_context["program"] = []
        plan_context["weeks"] = []
        plan_context["viewing"] = None
        # ``serialize_plan`` has ALREADY fallen back to a different block by
        # this point — ``open_week = current_week(plan, None)`` inside it
        # lands on the plan's earliest live week, quite possibly in block 1
        # while ``mesocycle`` (this block) is empty — and derives
        # ``current_mesocycle`` from THAT week, so ``phases`` marks block 1
        # "current" even though ``context["block"]`` is this empty block. That
        # is the same mixed-block leak the ``program``/``weeks``/``viewing``
        # blanks above exist to close, just surfacing on the macrocycle rail
        # instead of the grid. There's no meaningful "viewing" position for a
        # block with no live weeks, so clear it rather than reimplement
        # ``_phase_states`` here — an empty list renders no rail at all, which
        # is correct for "nothing to view."
        plan_context["phases"] = []

    context = {
        "plan": plan_context,
        "coach_style": _coach_style(plan.coach),
        "block": serializers.serialize_agent_block(plan, mesocycle),
    }
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
    plan,
    instruction,
    *,
    coach,
    mesocycle,
    trigger=models.AgentProposalBatch.Trigger.MANUAL,
):
    """Persist a ``drafting`` batch the endpoint returns before the job runs.

    ``mesocycle`` is the block the coach had open, captured by the caller at
    request time (``agent_propose`` / ``_reserve_plan_draft``) and frozen onto
    the batch (§4b) — grounding (a background job) and apply (a later request)
    read it back from here rather than re-deriving "which block" on their own,
    which is what let the answer silently drift to a different block than the
    one the coach was looking at. Required, not defaulted: a caller that
    doesn't know the block should resolve one explicitly (or pass ``None`` on
    purpose) rather than have this function guess.

    Snapshots the slicing dimensions for the usage ledger at creation: ``trigger``
    (what kicked the run off) and the coach's ``billing_status`` *now* (lossy to
    reconstruct later — COGS vs CAC). The usage/cost columns fill in once the job
    resolves the batch (``_persist_result`` / ``_fail``).
    """
    return models.AgentProposalBatch.objects.create(
        plan=plan,
        mesocycle=mesocycle,
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
                raw, batch.plan, mesocycle=batch.mesocycle, forbidden=forbidden
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
        "plan", "plan__relationship", "plan__relationship__athlete", "mesocycle"
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
                context=build_context(batch.plan, batch.mesocycle),
                instruction=batch.instruction,
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
    mesocycle,
    client=None,
    trigger=models.AgentProposalBatch.Trigger.MANUAL,
):
    """Run the agent synchronously and persist a reviewable batch.

    Blocking contract (kept for direct callers + tests): raises
    ``AgentNotConfigured`` when no client is available and ``AgentError`` on a
    provider failure — and persists no batch in either case. Returns
    ``(batch, rejected)`` on success. ``trigger`` tags the run for the usage ledger
    (the eval harness passes ``eval`` so its runs are excluded from cost reports).
    ``mesocycle`` is the block to ground/validate against (§4b) — required, like
    ``create_drafting_batch``, so a caller states its scope rather than this
    function guessing one.
    """
    client = client or client_module.get_default_client()
    if client is None:
        raise AgentNotConfigured("The Meso agent is not configured (no API key).")

    context = build_context(plan, mesocycle)
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
            mesocycle=mesocycle,
            coach=coach,
            instruction=instruction,
            status=models.AgentProposalBatch.Status.PENDING,
            trigger=trigger,
            billing_status=billing_access.billing_status(coach),
        )
        rejected = _persist_result(batch, result, model=model, duration_ms=duration_ms)

    return batch, rejected
