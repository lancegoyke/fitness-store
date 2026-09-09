"""Server-side verification for Cloudflare Turnstile.

Turnstile replaced Google reCAPTCHA v2 on the contact form. The v2 checkbox had
stopped being a real barrier -- commodity solving services clear it for a
fraction of a cent -- and every message that got through cost two outbound
emails, one of which echoed the sender's own text to an address of their
choosing.

The widget puts a token in the ``cf-turnstile-response`` POST field. That token
means nothing until it is traded with Cloudflare's ``siteverify`` endpoint,
which is the only party that can say whether a challenge was really passed.

Two checks beyond ``success`` matter here:

* **hostname** -- the sitekey is public, so anyone may embed this widget on
  their own page, farm solves there, and post the resulting tokens to us. Those
  tokens verify as ``success: true``. The hostname siteverify reports is what
  actually ties a token to this site, so a mismatch is rejected.
* **action** -- the label the widget stamps on its token, so one minted for some
  other surface cannot be replayed against the contact form. Checked only when
  Cloudflare echoes a non-empty action, since it is absent for a widget that
  does not set ``data-action``.

Everything here fails closed. A missing secret, a network problem, or a reply
that cannot be parsed all reject the submission rather than waving it through:
a contact form that is briefly unavailable is a smaller problem than one that
silently stops checking.
"""

import logging
from dataclasses import dataclass
from dataclasses import field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: The POST field the client-side widget writes its token into.
TOKEN_FIELD = "cf-turnstile-response"

#: siteverify blocks the request thread, so it gets a short budget.
VERIFY_TIMEOUT_SECONDS = 10

#: Locally-minted codes, kept distinct from Cloudflare's own ``error-codes``.
MISSING_SECRET = "missing-secret-key"
MISSING_TOKEN = "missing-input-response"
UNAVAILABLE = "verification-unavailable"
HOSTNAME_MISMATCH = "hostname-mismatch"
ACTION_MISMATCH = "action-mismatch"


@dataclass(frozen=True)
class TurnstileResult:
    """The verdict on a single submitted widget token."""

    success: bool
    error_codes: list[str] = field(default_factory=list)

    @property
    def is_misconfigured(self) -> bool:
        """Whether the failure is ours to fix rather than the visitor's."""
        ours = {MISSING_SECRET, "invalid-input-secret", "bad-request"}
        return bool(ours.intersection(self.error_codes))

    @property
    def is_unavailable(self) -> bool:
        """Whether Cloudflare could not be reached or understood."""
        return UNAVAILABLE in self.error_codes


def client_ip(request):
    """The visitor's IP address, or ``None`` if it cannot be determined.

    Production sits behind Caddy, so the first hop of ``X-Forwarded-For`` is the
    original client; ``REMOTE_ADDR`` covers a direct connection (local dev,
    tests). Passed to siteverify as the optional ``remoteip`` parameter.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _hostname_allowed(hostname: str) -> bool:
    """Whether the challenge was served from a host that belongs to us."""
    allowed = [host for host in settings.TURNSTILE_ALLOWED_HOSTNAMES if host]
    if not allowed:
        # Nothing to compare against (e.g. ALLOWED_HOSTS is "*"). Accept, but say
        # so loudly: this is the check that stops farmed tokens.
        logger.warning(
            "TURNSTILE_ALLOWED_HOSTNAMES is empty; skipping the hostname check."
        )
        return True
    return hostname in allowed


def verify(token: str, *, remote_ip: str | None = None) -> TurnstileResult:
    """Trade a widget token with Cloudflare for a pass/fail verdict.

    Args:
        token: the value the widget put in ``cf-turnstile-response``.
        remote_ip: the visitor's IP, sent as the optional ``remoteip``.

    Returns:
        A :class:`TurnstileResult`. Never raises: every failure path, including
        a network error, comes back as an unsuccessful result.
    """
    secret = settings.TURNSTILE_SECRET_KEY
    if not secret:
        logger.error(
            "TURNSTILE_SECRET_KEY is unset, so no submission can be verified. "
            "Set it in the environment."
        )
        return TurnstileResult(success=False, error_codes=[MISSING_SECRET])

    if not token:
        # No widget token at all: either a bot posting straight to the endpoint
        # or a visitor who submitted before the widget finished.
        return TurnstileResult(success=False, error_codes=[MISSING_TOKEN])

    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = requests.post(
            settings.TURNSTILE_ENDPOINT,
            data=payload,
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        logger.warning("Turnstile siteverify was unreachable.", exc_info=True)
        return TurnstileResult(success=False, error_codes=[UNAVAILABLE])

    if not isinstance(data, dict):
        logger.warning("Turnstile siteverify returned an unexpected payload.")
        return TurnstileResult(success=False, error_codes=[UNAVAILABLE])

    # ``error-codes`` is absent on success and can be null on failure, so it is
    # read defensively -- indexing it directly used to raise KeyError.
    error_codes = [str(code) for code in (data.get("error-codes") or [])]

    if data.get("success") is not True:
        return TurnstileResult(success=False, error_codes=error_codes)

    hostname = data.get("hostname") or ""
    if not _hostname_allowed(hostname):
        logger.warning(
            "Rejected a Turnstile token solved on %r, which is not one of %r. "
            "This is the signature of a farmed token.",
            hostname,
            settings.TURNSTILE_ALLOWED_HOSTNAMES,
        )
        return TurnstileResult(success=False, error_codes=[HOSTNAME_MISMATCH])

    action = data.get("action") or ""
    if action and action != settings.TURNSTILE_ACTION:
        logger.warning(
            "Rejected a Turnstile token minted for action %r, not %r.",
            action,
            settings.TURNSTILE_ACTION,
        )
        return TurnstileResult(success=False, error_codes=[ACTION_MISMATCH])

    return TurnstileResult(success=True)
