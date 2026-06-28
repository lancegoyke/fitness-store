"""Dispatch the proposal run off the request thread (Phase 4).

There is no task queue in this stack (Redis is cache/sessions only), and the
proposal job is a single short-lived call behind the human review gate, so a
daemon thread is the right-sized executor: the endpoint creates a ``drafting``
batch, ``dispatch_proposal`` hands the work to a thread, and the request returns
immediately. The frontend polls the batch's status endpoint until it resolves.

The thread boundary is deliberately thin — ``service.run_proposal_job`` already
never raises and always leaves the batch in a terminal state (``pending`` /
``failed``). Here we only need to not leak the thread's DB connection and to log
anything truly unexpected. ``MESO_AGENT_RUN_SYNC`` runs the job inline instead
(tests, and any environment that prefers a blocking call) so behavior is
deterministic without a thread.

Swapping in a real worker queue later is a drop-in: keep ``run_proposal_job`` as
the unit of work and replace this dispatch.
"""

import logging
import threading

from django.conf import settings
from django.db import connection
from django.db import transaction

from . import service

logger = logging.getLogger(__name__)


def dispatch_proposal(batch_id, *, client=None):
    """Run ``run_proposal_job`` for ``batch_id`` — inline when sync, else threaded."""
    if getattr(settings, "MESO_AGENT_RUN_SYNC", False):
        service.run_proposal_job(batch_id, client=client)
        return
    # ATOMIC_REQUESTS wraps the view in a transaction, so the drafting batch is
    # not committed (not visible to another connection) until the request ends.
    # Defer the thread to on_commit so it never queries the row before it lands.
    transaction.on_commit(lambda: _start_thread(batch_id, client))


def _start_thread(batch_id, client):
    thread = threading.Thread(
        target=_run,
        args=(batch_id,),
        kwargs={"client": client},
        name=f"meso-agent-{batch_id}",
        daemon=True,
    )
    thread.start()


def _run(batch_id, *, client=None):
    try:
        service.run_proposal_job(batch_id, client=client)
    except Exception:  # the thread has no caller to surface to
        logger.exception("Meso agent thread crashed for batch %s", batch_id)
    finally:
        # Don't leak the per-thread DB connection back into the pool.
        connection.close()
