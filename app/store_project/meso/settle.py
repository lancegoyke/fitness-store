"""24-hour settle sweep (5b, docs/meso/parse-at-commit-plan.md).

**Why.** 5a let a typed sub-line ("what you did") create a PENDING
``SessionLog`` with structured ``LoggedSet`` rows (``athlete_cell_write`` ->
``_upsert_parsed_set``, views.py), alongside the pre-existing structured "Save
progress"/"Log session" path (``athlete_log_session``). But most Meso reads
are **DONE-only by design** — adherence, the persisted ``AthleteOneRm``,
coach ``session_results``, and the agent's grounding all filter on
``status=DONE``. A session the athlete actually did, but only ever typed into
sub-lines and never tapped "Log session", was invisible to every one of those
— counted nowhere but the athlete's own page. This module is the fix: an
hourly sweep that promotes a PENDING log to DONE once it has gone quiet, on
the theory that "no more edits for a day" is as good a signal of "this
workout is finished" as the athlete tapping a button.

**What settles.** A ``SessionLog`` that is, ALL of:

1. ``status == PENDING``;
2. has at least one ``LoggedSet`` — parsed (typed) or structured ("Save
   progress"/"Log session"), the two are treated identically here, exactly as
   every DONE-only reader already treats them once a log is DONE;
3. ``last_activity_at <= now - MESO_SETTLE_QUIET_HOURS`` (bumped by both
   athlete write paths on a real edit — see their own docstrings/comments);
4. is the **newest** log for its ``(session, athlete)`` pair, by
   ``-created_at`` — the same rule every read site already uses
   (``SessionLog.objects.filter(session=..., athlete=...).order_by
   ("-created_at").first()``). An older duplicate (however it got there) is
   dead weight, never the athlete's live record, and must never settle.

A notes-only / zero-set PENDING log never settles (rule 2) — there is nothing
for it to promote into a DONE-only read. Settling does not care whether the
session/plan is still reachable by the athlete (an archived plan, a
soft-deleted session, an ended coach link): the athlete did the work, and a
later cleanup elsewhere is a separate concern from "did this count".

**The lock contract.** ``settle_log`` takes
``Session.objects.select_for_update()`` on the log's session — the *exact*
row, in the *exact* place (before anything else is read), that both athlete
write paths already lock first (``athlete_log_session`` and
``athlete_cell_write``, views.py). That one shared lock is what lets all
three writers (the two views and this sweep) treat "read the newest log,
maybe create/update it" as atomic with respect to each other; without it, a
sweep and a concurrent blur could each act on a state the other has since
made stale. Because the sweep's candidate list is built *outside* any
transaction (so one hourly run can iterate many logs without holding locks
between them), ``settle_log`` re-reads the log and re-checks **every** rule
above from scratch once the lock is held — a candidate selected a moment ago
may no longer qualify (a blur bumped it, "Log session" already completed it,
a newer log appeared, its sets were cleared) — and simply declines rather
than acting on stale information.

**Deliberately NOT here** (follow-ups, not this slice):

- no notification (push/email) telling the athlete/coach a session settled;
- no persisted "confirmed" snapshot distinct from ``SessionLog``/``LoggedSet``
  themselves (e.g. a ``PersonalRecord`` row) — PRs are still derived on read;
- retiring the structured logger ("Save progress"/"Log session") now that
  typed-then-quiet also counts — out of scope, a UX decision for later.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Subquery
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.track import track

from . import one_rm as meso_one_rm
from . import tour as meso_tour
from .models import ExerciseSlot
from .models import LoggedSet
from .models import Session
from .models import SessionLog
from .models import newest_session_logs

logger = logging.getLogger(__name__)


def quiet_period():
    """How long a PENDING log must go untouched before it's eligible to settle."""
    return timedelta(hours=settings.MESO_SETTLE_QUIET_HOURS)


def settleable_logs(cutoff):
    """PENDING logs quiet since ``cutoff`` that are their pair's newest, with sets.

    ``Exists``/``Subquery`` rather than joins + ``distinct``: a join against
    ``sets`` would multiply rows per ``LoggedSet`` (needing a ``distinct``
    that's easy to get subtly wrong once another join is added later), and
    "the newest log for this (session, athlete) pair" isn't a per-row join
    predicate at all — it's "no other row for the same pair has a later
    ``created_at``", which is naturally a correlated subquery.

    The pair-newest ordering is the shared rule — see
    ``models.newest_session_logs`` for the ordering rationale and the full
    list of reads (including ``settle_log`` below) that share it. Without it,
    two logs sharing a ``created_at`` (precisely the split-log rows #568
    exists for) would sort ambiguously here, and this query could pick a
    different one of the pair than those other reads do, settling a log the
    athlete-facing surfaces never treat as current.
    """
    newest_pk_for_pair = newest_session_logs(
        OuterRef("session"), OuterRef("athlete")
    ).values("pk")[:1]
    has_a_logged_set = LoggedSet.objects.filter(session_log=OuterRef("pk"))
    return SessionLog.objects.filter(
        status=SessionLog.Status.PENDING,
        last_activity_at__lte=cutoff,
        pk=Subquery(newest_pk_for_pair),
    ).filter(Exists(has_a_logged_set))


def settle_log(pk, *, cutoff):
    """Settle one candidate log to DONE if it's still settleable under the lock.

    Returns whether it actually settled. See the module docstring for the
    lock contract and why every rule is re-checked here rather than trusted
    from the caller's candidate list.
    """
    with transaction.atomic():
        session_id = (
            SessionLog.objects.filter(pk=pk)
            .values_list("session_id", flat=True)
            .first()
        )
        if session_id is None:
            return False  # already gone (e.g. a history restore, an admin delete)

        # THE lock — see the module docstring. Same row, same "before anything
        # else" ordering, as both athlete write paths (views.py's
        # `athlete_log_session` and `athlete_cell_write`): whichever of the
        # sweep or a concurrent blur/save gets here first makes the other
        # wait, so the loser's re-read below always sees the winner's
        # committed result rather than racing it.
        Session.objects.select_for_update().filter(pk=session_id).first()

        # Re-read and re-verify EVERY settleable_logs() condition now that the
        # lock is held. The candidate list was built outside any transaction,
        # so anything could have happened to this exact row in the meantime: a
        # blur bumped `last_activity_at`, "Log session" already completed it,
        # a newer log for this (session, athlete) pair now exists, or its sets
        # were cleared. Any of those means "not settleable, right now" — leave
        # the row exactly as it is and let the next hourly pass re-judge it on
        # its own, then-current merits.
        #
        # The log row itself is locked too (Session first, then the log — the
        # order the write paths take them in), and it must still belong to the
        # session we locked: `session_id` was read before that lock, and an
        # admin reassigning the log in between would leave us guarding the
        # wrong session while a blur on the new one edits it.
        log = (
            SessionLog.objects.filter(pk=pk)
            .select_related("session__week__mesocycle__plan", "athlete")
            .select_for_update(of=("self",))
            .first()
        )
        if log is None or log.session_id != session_id:
            return False
        # The shared newest-log rule — see models.newest_session_logs.
        newest = newest_session_logs(log.session_id, log.athlete_id).first()
        if newest is None or newest.pk != log.pk:
            return False
        if log.status != SessionLog.Status.PENDING:
            return False
        if log.last_activity_at > cutoff:
            return False
        if not log.sets.exists():
            return False

        # Do NOT touch `last_activity_at` or `date` — this is a status flip
        # only, not an athlete edit, and both fields are meant to keep
        # recording the athlete's own activity/workout-day, not the sweep's.
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])

        # Only THIS log's sets can have changed what DONE-only derivation sees
        # — refresh exactly the lifts they reference, not the whole session
        # (which could pull in unrelated already-DONE history unnecessarily,
        # though harmlessly; scoping it is simply precise about what changed).
        #
        # #578 C1: collected via ``anchor_slot_id``, not ``prescription_id`` —
        # a set whose ``prescription`` went NULL (a hard-deleted line-0 cell,
        # #577/#581) but whose ``exercise_slot`` survives must still have its
        # lift refreshed. ``select_related("prescription")`` makes the
        # fallback hop (``anchor_slot_id``'s read of ``prescription.
        # exercise_slot_id``) free instead of one query per set.
        anchor_slot_ids = {
            ls.anchor_slot_id
            for ls in log.sets.select_related("prescription")
            if ls.anchor_slot_id is not None
        }
        lifts = list(ExerciseSlot.objects.filter(pk__in=anchor_slot_ids))
        meso_one_rm.refresh_one_rms(
            log.athlete, lifts, log.session.week.mesocycle.plan.unit
        )

    # Outside the atomic block, like `athlete_log_session`'s own call to this:
    # a tour failure must never roll back a settle that already committed.
    try:
        meso_tour.advance_self_step_if_complete(log.athlete, "results")
    except Exception:
        logger.exception("meso settle: tour advance failed for SessionLog %s", pk)
    track(EventName.SESSION_COMPLETED, actor=log.athlete, subject=log, via="settle")
    return True


def settle_quiet_logs(now=None):
    """Settle every currently-quiet PENDING log; returns how many settled.

    The candidate pks are materialized up front (a plain list, no open
    transaction) precisely so one bad log's failure — caught here, not inside
    ``settle_log`` — can never wedge the rest of an hourly run.
    """
    cutoff = (now or timezone.now()) - quiet_period()
    pks = list(settleable_logs(cutoff).values_list("pk", flat=True))
    settled = 0
    for pk in pks:
        try:
            if settle_log(pk, cutoff=cutoff):
                settled += 1
        except Exception:
            logger.exception("meso settle: failed to settle SessionLog %s", pk)
            continue
    logger.info("Settled %d quiet PENDING log(s).", settled)
    return settled
