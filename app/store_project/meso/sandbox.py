"""Public, no-signup ephemeral coach sandbox (issue #389).

A logged-out visitor to ``/meso/demo/`` gets a real, throwaway coach ``User`` —
marked with a ``SandboxSession`` — logged in for the length of their visit, so
every existing login-gated view / CSRF / scoping query just works. The
workspace starts **populated** (#650 — five athletes, a built program, a
delivered week, a logged session, via ``demo.load_demo``); the guided demo
onboarding tour (``tour.py``, issue #430 Phase 2) is still armed and narrates
that loaded workspace. Phase 2 (of *this* module) adds
the expiry sweep that reaps a sandbox after its TTL. See
``docs/meso/public-sandbox-demo-plan.md`` and
``docs/meso/demo-onboarding-tour-plan.md``.
"""

import logging
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import connection
from django.db import transaction
from django.utils import timezone

from store_project.users.models import User

from . import demo
from . import tour
from .models import CoachProfile
from .models import SandboxSession

logger = logging.getLogger(__name__)

#: Non-routable (RFC 6761 ``.invalid``) sandbox-coach domain — never real mail.
SANDBOX_EMAIL_DOMAIN = "sandbox.invalid"


#: Arbitrary constant key for the creation-serializing advisory lock (#673).
_CREATE_LOCK_KEY = 0x5A4D_0B67


class SandboxBusy(Exception):
    """The global sandbox cap is reached; no sandbox was minted (#673)."""


def at_capacity():
    """Cheap, UNLOCKED cap check — a fast path only, never the bound (#673)."""
    return SandboxSession.objects.count() >= settings.MESO_SANDBOX_MAX_CONCURRENT


def is_sandbox(user):
    """Whether ``user`` is a throwaway sandbox coach. False for anonymous/None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return SandboxSession.objects.filter(user=user).exists()


def create_sandbox(*, source_ip=None):
    """Mint a throwaway coach: ``User`` + ``CoachProfile`` + demo data + a tour.

    Unusable password (never a real login credential) and a non-routable,
    per-visitor email (never real mail) mark the account as disposable. The
    workspace is **populated** up front (#650, the landing page's "populated
    workspace" promise): ``demo.load_demo`` runs inside this same transaction —
    silent, no email/push — and the guided tour is still armed at step 0, where
    each step narrates the already-loaded segment. Returns the new user.

    The global cap (``MESO_SANDBOX_MAX_CONCURRENT``) is authoritative HERE, not
    in the view (#673): a transaction-scoped advisory lock serializes creators,
    and the count is read only after it is held, so concurrent entries cannot
    overshoot. Raises ``SandboxBusy`` at the cap, creating nothing. The lock is
    held to commit — i.e. through the demo load — so entries queue briefly; that
    is the price of an exact bound on a public, unauthenticated mint. A
    key-level advisory lock, not a row lock: it names no row, so it sits outside
    the User -> CoachAthlete -> ... order and cannot join a cycle.
    """
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_CREATE_LOCK_KEY])
        if at_capacity():
            raise SandboxBusy
        return _create_sandbox_locked(source_ip)


def _create_sandbox_locked(source_ip):
    email = f"{uuid4().hex}@{SANDBOX_EMAIL_DOMAIN}"
    user = User.objects.create(email=email, username=email, name="Demo Coach")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    profile, _ = CoachProfile.objects.get_or_create(user=user)
    SandboxSession.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(hours=settings.MESO_SANDBOX_TTL_HOURS),
        source_ip=source_ip,
    )
    demo.load_demo(user)
    tour.start_tour(profile)
    tour.record_started(user, "sandbox")
    return user


def expire_sandboxes(now=None):
    """Reap every sandbox whose TTL has passed; returns how many were reaped.

    Order matters: the demo athletes are **separate** ``User`` rows with no FK
    cascade from the coach, so ``demo.clear_demo`` must run first (it deletes
    the demo-athlete users and the demo group explicitly) — only then does
    deleting the coach user cascade the rest (``CoachProfile``,
    ``SandboxSession``, any remaining coach-scoped rows). A cascade-only sweep
    would leak five orphaned users per sandbox.

    Best-effort per sandbox: one bad row is logged and skipped (left for the
    next hourly run), never wedging the whole sweep.
    """
    cutoff = now or timezone.now()
    reaped = 0
    overdue = SandboxSession.objects.filter(expires_at__lte=cutoff).select_related(
        "user"
    )
    for session in overdue:
        try:
            # Heavy lifting first, outside the delete transaction. Not enough on
            # its own: see the re-clear under the coach lock below (#674).
            demo.clear_demo(session.user)
            # #559: the coach delete cascades too — their plans, their
            # ``AgentProposalBatch`` rows (a CASCADE FK straight to ``User``,
            # as well as through the plans) and everything under them — so it
            # takes the same parent locks first, for the same reason
            # ``clear_demo`` does. Its own ``atomic``, not one wrapping both
            # calls: the sweep is best-effort PER SANDBOX, and pairing them in
            # one transaction would mean a failure on the coach delete rolled
            # back the demo clear that had already succeeded.
            with transaction.atomic():
                # #674: the sandbox is still logged in, so a ``demo_load`` POST
                # can land between the clear above and this transaction and
                # recreate the demo athletes — separate User rows no cascade
                # reaches. Take the coach mutex first (every loader takes it),
                # then clear again under it: no load can interleave after this.
                # A loader queued behind us wakes to a deleted coach row and
                # fails, rolling back, rather than leaving orphans.
                user = (
                    User.objects.select_for_update(no_key=True)
                    .filter(pk=session.user_id)
                    .first()
                )
                if (
                    user is None
                    or not SandboxSession.objects.filter(pk=session.pk).exists()
                ):
                    continue  # reaped elsewhere meanwhile; nothing to do
                demo.clear_demo(user)
                demo.lock_cascade_parents([user.pk])
                user.delete()
        except Exception:  # reaping is best-effort; never wedge the sweep
            logger.exception("Failed to reap sandbox for user %s", session.user_id)
            continue
        reaped += 1
    logger.info("Reaped %d expired sandbox(es).", reaped)
    return reaped
