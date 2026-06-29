"""Dispatch the proposal run onto the django-q cluster (Phase 4 → app queue).

The proposal job is a single short-lived call behind the human review gate. It
used to run on a bare daemon thread because the stack had no task queue; now it
has django-q2 — the ORM-broker ``qcluster`` that already runs the invite sweeps —
so the job runs as a real queued task instead of a thread we hand-manage.

The endpoint creates a ``drafting`` batch and ``dispatch_proposal`` enqueues
``run_proposal_job`` for the cluster to pick up; the request returns immediately
and the frontend polls the batch's status endpoint until it resolves.

``service.run_proposal_job`` is the unit of work — it never raises and always
leaves the batch in a terminal state (``pending`` / ``failed``), so the queue
wrapper stays thin. Only the batch id is enqueued: the worker is a separate
process that reconstructs its own Claude client (``get_default_client``), and a
client isn't picklable anyway.

``MESO_AGENT_RUN_SYNC`` runs the job inline instead of enqueuing it (tests, and
any environment that prefers a blocking, queue-free call) so behavior is
deterministic without a worker. The test settings also run django-q in ``sync``
mode, so an enqueued task there executes in-process.
"""

import logging

from django.conf import settings
from django.db import transaction
from django_q.tasks import async_task

from . import service

logger = logging.getLogger(__name__)

# Dotted path django-q stores and imports in the worker process. It must keep
# pointing at the unit of work; a rename that misses this string would break
# dispatch silently in production (a test runs the enqueued task end to end under
# sync mode to catch exactly that).
RUN_PROPOSAL_TASK = "store_project.meso.agent.service.run_proposal_job"


def dispatch_proposal(batch_id, *, client=None):
    """Run ``run_proposal_job`` for ``batch_id`` — inline when sync, else queued."""
    if getattr(settings, "MESO_AGENT_RUN_SYNC", False):
        service.run_proposal_job(batch_id, client=client)
        return
    # ATOMIC_REQUESTS wraps the view in a transaction. Enqueue on commit so the
    # task lands only once the drafting batch has durably committed (a worker in
    # another process would otherwise race the row) and never if the request rolls
    # back.
    transaction.on_commit(lambda: _enqueue(batch_id))


def _enqueue(batch_id):
    """Hand the job to the cluster; resolve the batch if the broker write fails."""
    try:
        async_task(RUN_PROPOSAL_TASK, batch_id)
    except Exception:  # a broker failure must not strand the batch in ``drafting``
        logger.exception("Meso agent failed to enqueue job for batch %s", batch_id)
        _fail_unqueued(batch_id)


def _fail_unqueued(batch_id):
    """Mark a batch ``failed`` when its job could not be queued (no worker will).

    A bare ``update`` so it can't fail on a stale in-memory row and never widens
    the change beyond the status the status-poll surfaces.
    """
    from .. import models

    models.AgentProposalBatch.objects.filter(pk=batch_id).update(
        status=models.AgentProposalBatch.Status.FAILED,
        error="The agent run could not be queued.",
    )
