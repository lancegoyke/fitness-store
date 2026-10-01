"""Which invite is this browser following? (#642).

The anonymous claim page records the token in the session; allauth's signup page
reads it back to prefill and brand itself, and the authenticated claim GET uses
it to accept without a second click. Only the anonymous claim page of *that*
token ever sets it, so a logged-in visitor clicking a crafted link is never
accepted without their click.
"""

from .models import CoachInvite

CLAIM_SESSION_KEY = "meso_claim_token"


def remember_claim(request, invite):
    request.session[CLAIM_SESSION_KEY] = str(invite.token)


def forget_claim(request):
    request.session.pop(CLAIM_SESSION_KEY, None)


def session_claim_token(request):
    return request.session.get(CLAIM_SESSION_KEY)


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
