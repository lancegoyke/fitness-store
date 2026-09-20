"""The client beacon endpoint (#509 slice 3).

``track_beacon`` is the one place browser-only facts enter the ledger — a
PWA install, a push-permission answer, a push click reported by a page that
somehow missed the server-side path. It's deliberately thin: parsing and
rate limiting live here because they're HTTP concerns, but *which* names and
props are acceptable at all is ``analytics.beacon``'s closed table, so that
policy stays testable without a request object.

No ``@login_required``: a redirect to the login page is useless to a
``fetch()`` call (the browser would just follow it and get an HTML login
form back, which isn't JSON and isn't a 204), and there's nothing an
anonymous beacon post could mean — every accepted event is about *this
device's* relationship to *this account* (installed the app, answered a
permission prompt), which doesn't exist yet for a visitor who hasn't signed
in. Dropping it here, silently, is also the answer to issue #542's open
question about the beacon and anonymous actors: it's dropped, not queued,
not attributed to a session.

CSRF is left to Django's standard ``CsrfViewMiddleware`` — this view is not
``@csrf_exempt``. Unlike ``notifications.views.unsubscribe_delivery_email``
(a one-click email-client POST that can never carry a token), every beacon
call originates from our own JS running on our own page, which can read the
CSRF cookie same as any other same-origin fetch in this app.
"""

import json

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from . import beacon
from .models import Event
from .track import track


def _rate_limited(user):
    """Whether this user has posted too many beacon events this hour.

    Same cache-counted idiom as ``meso.views._sandbox_rate_limited`` (read
    that one first) — ``cache.add`` seeds the counter with its one-hour TTL
    (a no-op once it exists), then ``incr`` bumps it, which on Redis
    preserves the existing TTL rather than resetting it on every hit.

    That makes it a **fixed** window anchored at the first post of the hour,
    not a sliding one: a client that spends its budget just before the TTL
    lapses and again just after gets through roughly twice the limit in a
    short span. That's accepted rather than fixed with overlapping buckets —
    this bounds a misbehaving page, and 120 tiny events in a bad minute is
    not a number worth a second cache key. Keyed per user rather than
    per IP, unlike the sandbox limiter: a beacon post always carries an
    authenticated session (see ``track_beacon``), so the account is the
    natural, stable bound — an IP key would either conflate every signed-in
    member of one household or, on a shared/corporate NAT, one bad tab
    could rate-limit everyone behind it.
    """
    key = f"analytics:beacon:rate:{user.pk}"
    cache.add(key, 0, timeout=3600)
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between `add` and `incr` (the window rolled over
        # mid-call). Treat it as the first event of a fresh window.
        cache.set(key, 1, timeout=3600)
        count = 1
    return count > settings.ANALYTICS_BEACON_PER_USER_PER_HOUR


def _json_object_body(request):
    """``request.body`` parsed as a JSON object, or the 400 to send instead.

    A local copy of the parsing half of ``meso.views._json_object_body``
    (read that one first for the shared idiom) rather than an import —
    analytics must not depend on meso's view module, and this endpoint has
    none of that helper's bodyless-write tolerance to preserve. The
    designer's PATCH-style endpoints treat a missing body as a legitimate
    "no changes" no-op; the beacon has no such case, every real call posts
    ``{"name": ..., "props": ...}`` as JSON, so an empty body is simply
    unparseable JSON like any other and falls out of the same
    ``json.JSONDecodeError`` branch rather than needing its own check.

    Returns ``(payload, None)`` on success or ``(None, response)`` on
    failure, so a caller can write ``payload, bad = _json_object_body(...);
    if bad is not None: return bad``.
    """
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"ok": False, "error": "Malformed JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse(
            {"ok": False, "error": "Expected a JSON object."}, status=400
        )
    return payload, None


@require_POST
def track_beacon(request):
    """Record one browser-only usage event posted by ``meso_track.js``.

    Checks run in a fixed order, each chosen to fail as cheaply as possible
    before doing more work on a request that's already going nowhere:

    1. anonymous → 204, nothing recorded (see the module docstring).
    2. the rate limit → 429. First, so it counts every authenticated
       *attempt* — the same reasoning as ``meso.views._sandbox_rate_limited``
       ("counts attempts, so hammering past the limit never re-opens it
       early"). A limiter that only counted well-formed posts would let a
       client send malformed ones at no budget cost, which is exactly the
       traffic it most wants to bound.
    3. a content type other than ``application/json`` → 415. This must come
       **before** anything reads ``request.body``. ``CsrfViewMiddleware``
       looks for ``csrfmiddlewaretoken`` in ``request.POST`` before it falls
       back to the ``X-CSRFToken`` header, and for ``multipart/form-data``
       that parse consumes the stream without stashing ``_body`` — so a
       later ``request.body`` raises ``RawPostDataException`` and the view
       500s. The beacon only ever speaks JSON, so saying so plainly is both
       the right contract and the fix.
    4. an oversized body → 400, before it's read as JSON.
    5. a body that isn't a JSON object → 400.
    6. ``beacon.validate`` → 400 with its returned reason.
    7. ``track()``, with ``source=Event.Source.CLIENT`` — the one call site
       in the codebase allowed to pass that, because this is the one place
       a fact reaches us because the *browser* reported it rather than
       because our own code observed it server-side.

    Success is a bare 204: nothing on the page reads a beacon call's
    response (``meso_track.js`` fires-and-forgets), so there's no reason to
    spend a body on ``{"ok": true}``.
    """
    if not request.user.is_authenticated:
        return HttpResponse(status=204)
    if _rate_limited(request.user):
        return JsonResponse({"ok": False, "error": "Too many events."}, status=429)
    if request.content_type != "application/json":
        return JsonResponse(
            {"ok": False, "error": "Expected application/json."}, status=415
        )
    if len(request.body) > beacon.MAX_BODY_BYTES:
        return JsonResponse({"ok": False, "error": "Body too large."}, status=400)
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad
    name, result = beacon.validate(payload.get("name"), payload.get("props"))
    if name is None:
        return JsonResponse({"ok": False, "error": result}, status=400)
    track(name, actor=request.user, source=Event.Source.CLIENT, **result)
    return HttpResponse(status=204)
