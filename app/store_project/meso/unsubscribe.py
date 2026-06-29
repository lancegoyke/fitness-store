"""Meso delivery-email unsubscribe — tokened, login-free opt-out.

The delivered-week email is the one transactional message a coached athlete
receives, and until now it had no off switch (web push is opt-in via the browser
permission; email was not). This adds the email best-practice: a one-click
``List-Unsubscribe`` link that actually works.

The link carries a signed token (``django.core.signing``) naming the athlete —
no login required (the recipient may not be signed in, and a social login may use
a different address than the one we mailed) and no token column (the signature is
the authorization). Following it records a single opt-out flag on the athlete's
``AthleteProfile``; the deliver hook (``views._notify_athlete_delivered``) honors
it. Intentionally *not* a notification-preferences system — one flag for the one
email that needed an off switch.
"""

from django.contrib.auth import get_user_model
from django.core import signing

from .models import AthleteProfile

# Namespaces the signature to this purpose so a token can't be replayed against
# any other signed context that happens to share the secret key.
_SALT = "meso.delivery-email-unsubscribe"


def make_unsubscribe_token(user) -> str:
    """A signed, URL-safe token naming ``user`` for the unsubscribe link."""
    return signing.dumps(str(user.pk), salt=_SALT)


def resolve_unsubscribe_user(token: str):
    """The ``User`` a token names, or ``None`` if it is invalid/tampered.

    No ``max_age`` — an unsubscribe link must never expire (someone may act on an
    old email). A valid signature means we minted the token from a real user pk,
    so the subsequent lookup is safe.
    """
    try:
        uid = signing.loads(token, salt=_SALT)
    except signing.BadSignature:
        return None
    return get_user_model().objects.filter(pk=uid).first()


def athlete_opted_out(user) -> bool:
    """True if ``user`` has opted out of training-delivery emails."""
    return AthleteProfile.objects.filter(
        user=user, delivery_email_opt_out=True
    ).exists()


def set_delivery_email_opt_out(user, opted_out: bool) -> None:
    """Record the athlete's delivery-email opt-out, creating a profile if needed."""
    profile, _ = AthleteProfile.objects.get_or_create(user=user)
    if profile.delivery_email_opt_out != opted_out:
        profile.delivery_email_opt_out = opted_out
        profile.save(update_fields=["delivery_email_opt_out", "modified"])
