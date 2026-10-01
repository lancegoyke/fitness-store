"""Which invite is this browser following? (#642).

The anonymous claim page records the token in the session; allauth's signup page
reads it back to prefill and brand itself, and the authenticated claim GET uses
it to accept without a second click. Only the anonymous claim page of *that*
token ever sets it, so a logged-in visitor clicking a crafted link is never
accepted without their click.

The flag lives for ``CLAIM_TTL_SECONDS`` and is cleared by any login that is not
heading back to that token's claim page (#670), so a flag left behind by an
abandoned signup cannot auto-accept after some unrelated login. allauth flows
decide that from the redirect response; every other login (the admin, the
sandbox entry, any ``login()`` call) clears it unless its ``next`` is the claim
page (#677).
"""

import posixpath
import time
from urllib.parse import urlsplit

from django.dispatch import receiver
from django.urls import NoReverseMatch
from django.urls import reverse

from .models import CoachInvite

CLAIM_SESSION_KEY = "meso_claim_token"
CLAIM_AT_SESSION_KEY = "meso_claim_at"
CLAIM_TTL_SECONDS = 2 * 60 * 60


def remember_claim(request, invite):
    request.session[CLAIM_SESSION_KEY] = str(invite.token)
    request.session[CLAIM_AT_SESSION_KEY] = time.time()


def forget_claim(request):
    request.session.pop(CLAIM_SESSION_KEY, None)
    request.session.pop(CLAIM_AT_SESSION_KEY, None)


def session_claim_token(request):
    """The flagged token, or ``None`` when absent, undated or older than the TTL."""
    token = request.session.get(CLAIM_SESSION_KEY)
    if not token:
        return None
    stamped = request.session.get(CLAIM_AT_SESSION_KEY)
    try:
        age = time.time() - float(stamped)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > CLAIM_TTL_SECONDS:
        return None
    return token


def _clear_unless_claim_redirect(request, response):
    """Drop the flag unless this login is redirecting to the flagged claim page.

    Called from allauth's ``user_logged_in`` (which carries the response, so the
    final redirect target is known for password login, signup and social login
    alike). The signup/login that follows the claim page redirects straight back
    to ``/meso/claim/<token>/`` and keeps the flag; anything else is unrelated.
    """
    token = request.session.get(CLAIM_SESSION_KEY)
    if not token:
        return
    try:
        claim_path = reverse("meso:invite_claim", kwargs={"token": token})
    except (NoReverseMatch, ValueError, TypeError):
        forget_claim(request)
        return
    try:
        target = urlsplit(response["Location"]).path
    except (KeyError, TypeError, AttributeError, ValueError):
        target = ""
    if target != claim_path:
        forget_claim(request)


def session_claim_invite(request):
    """The still-claimable invite this session is following, else ``None``.

    A stale flag (answered, revoked, expired, rotated by a resend) resolves to
    nothing, so callers never reveal anything about a dead invite.
    """
    token = session_claim_token(request)
    if not token:
        return None
    try:
        invite = (
            CoachInvite.objects.select_related("coach", "coach__coach_profile")
            .filter(token=token)
            .first()
        )
    except (ValueError, TypeError):  # malformed value in the session
        return None
    if invite is None or not invite.is_claimable:
        return None
    return invite


def _connect():
    # allauth's own signal (not ``django.contrib.auth``'s): it is sent after the
    # login with the redirect response, so the destination is known.
    from allauth.account.signals import user_logged_in

    @receiver(user_logged_in, weak=False)
    def clear_claim_on_unrelated_login(sender, request=None, response=None, **kwargs):
        if request is not None and hasattr(request, "session"):
            _clear_unless_claim_redirect(request, response)

    from django.contrib.auth.signals import user_logged_in as django_user_logged_in

    @receiver(django_user_logged_in, weak=False)
    def clear_claim_on_django_only_login(sender, request=None, **kwargs):
        # Django's signal also fires for every allauth login, BEFORE allauth's
        # own receiver above, which alone knows the redirect target. So leave
        # allauth flows (login, signup, social callbacks — all under its URL
        # prefix) to that receiver, keep the flag when this login says it is
        # heading back to the claim page, and clear it for everything else (the
        # /backside/ admin, the sandbox entry, a bare ``login()``) — #677.
        if request is None or not hasattr(request, "session"):
            return
        token = request.session.get(CLAIM_SESSION_KEY)
        if not token:
            return
        if request.path.startswith(_allauth_prefix()):
            return
        try:
            claim_path = reverse("meso:invite_claim", kwargs={"token": token})
        except (NoReverseMatch, ValueError, TypeError):
            claim_path = None
        requested = request.POST.get("next") or request.GET.get("next") or ""
        if claim_path and urlsplit(requested).path == claim_path:
            return
        forget_claim(request)


def _allauth_prefix():
    """The URL prefix allauth is mounted under (``/accounts/``)."""
    return posixpath.dirname(reverse("account_login").rstrip("/")) + "/"


_connect()
