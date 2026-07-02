"""Public, no-signup ephemeral coach sandbox (issue #389, Phase 1).

A logged-out visitor to ``/meso/demo/`` gets a real, throwaway coach ``User`` —
seeded via ``demo.load_demo`` and marked with a ``SandboxSession`` — logged in
for the length of their visit, so every existing login-gated view / CSRF /
scoping query just works. See ``docs/meso/public-sandbox-demo-plan.md``.
"""

from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from store_project.users.models import User

from . import demo
from .models import CoachProfile
from .models import SandboxSession

#: Non-routable (RFC 6761 ``.invalid``) sandbox-coach domain — never real mail.
SANDBOX_EMAIL_DOMAIN = "sandbox.invalid"


def is_sandbox(user):
    """Whether ``user`` is a throwaway sandbox coach. False for anonymous/None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return SandboxSession.objects.filter(user=user).exists()


@transaction.atomic
def create_sandbox(*, source_ip=None):
    """Mint a throwaway coach: ``User`` + ``CoachProfile`` + seeded demo data.

    Unusable password (never a real login credential) and a non-routable,
    per-visitor email (never real mail) mark the account as disposable; the
    workspace is populated immediately via ``demo.load_demo`` so the visitor has
    something to explore. Returns the new user.
    """
    email = f"{uuid4().hex}@{SANDBOX_EMAIL_DOMAIN}"
    user = User.objects.create(email=email, username=email, name="Demo Coach")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    CoachProfile.objects.get_or_create(user=user)
    SandboxSession.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(hours=settings.MESO_SANDBOX_TTL_HOURS),
        source_ip=source_ip,
    )
    demo.load_demo(user)
    return user
