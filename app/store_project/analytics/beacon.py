"""The closed set of client-reportable events (#509 slice 3).

A pure module — no Django request, no database — so the beacon's
accept/reject policy is one small table that's unit-testable on its own,
without spinning up a client or touching ``Event``. ``analytics.views.
track_beacon`` is the only caller; it owns the HTTP plumbing (body size,
JSON parsing, rate limiting, CSRF) and hands this module exactly the two
things a POST body decomposes into — ``name`` and ``props`` — asking only
"is this one of the events we chose to expose to the browser".

Why closed rather than open, i.e. why the beacon can't just post any
``EventName`` plus arbitrary JSON the way a server-side ``track()`` call
can: every server-side call site is our own code, so a bad prop there only
ever costs a bug we wrote and can fix. This endpoint is reachable from any
tab that loaded the page, logged in or not (well — logged in; see
``views.track_beacon`` for why anonymous is dropped before this module is
even consulted), so whatever it accepts has to be enumerable in advance.
``CLIENT_EVENTS`` is that enumeration: a client can only *choose among*
values we already named, never compute one — no free-form strings, no
URLs, no user agent, nothing a page could use to smuggle arbitrary text
into the ledger.
"""

from .events import EventName

#: Every accepted prop is one short string from a closed set. No free-form
#: values, no URLs, no user agent — a client can only choose among values we
#: named here, so nothing a page can compute ends up in the ledger.
#:
#: ``push_clicked`` is deliberately **not** here, even though it is one of the
#: browser-only names in ``events.py``. The server writes it, from the
#: notification's landing URL, once per ledger row
#: (``notifications.push.record_push_click``), and nothing in the app ever
#: posts it. Accepting it anyway would have been pure inbound surface with no
#: caller — and it would have broken the thing that makes the number worth
#: reading: today a ``push_clicked`` event implies a ``PushNotification`` row
#: that was actually clicked, and any signed-in browser could otherwise have
#: added events with no row behind them.
CLIENT_EVENTS = {
    EventName.PWA_INSTALLED: {"via": {"appinstalled", "standalone"}},
    EventName.PUSH_PERMISSION: {"result": {"granted", "denied", "default"}},
}

#: Props a name cannot be recorded without. Anything not listed here is
#: optional for that name — most of ``CLIENT_EVENTS`` has no such entry.
REQUIRED_PROPS = {EventName.PUSH_PERMISSION: ("result",)}

#: Bytes of request body the beacon will read. The largest legitimate post
#: (e.g. ``{"name": "push_permission", "props": {"result": "denied"}}``) is
#: well under 100 bytes; this is a bound against a hostile or broken client,
#: not a budget any real page is meant to approach.
MAX_BODY_BYTES = 512


def validate(name, props):
    """Return ``(name, props)`` for a recordable client event, or ``(None, error)``.

    ``error`` is a short human string meant to go straight into the 400
    body — there's no separate error-code taxonomy, because the only
    consumer is a developer reading Network tab output while wiring up a
    new beacon call, not end-user copy.

    Checks run in this fixed order so the error a bad client sees is always
    the *first* thing wrong with the payload, not whichever check happened
    to run last:

    1. is ``name`` one of the events we chose to expose at all? (checked
       before anything about ``props`` is even looked at — an unknown name
       makes every other question moot)
    2. is ``props`` shaped like an object? (a missing ``props`` is treated
       as ``{}`` — "no props" and "an empty props object" mean the same
       thing to every caller here)
    3. is every key in ``props`` one this name actually accepts?
    4. is every value a string drawn from that key's closed set?
    5. is every prop this name requires actually present?

    Kept total and boring on purpose: no regexes, no separate length
    checks. Set membership already bounds both the shape and the length of
    every legal value, because every legal value is spelled out in
    ``CLIENT_EVENTS`` rather than pattern-matched.
    """
    if not isinstance(name, str) or name not in CLIENT_EVENTS:
        return None, "Unknown event name."
    if props is None:
        props = {}
    if not isinstance(props, dict):
        return None, "props must be an object."
    allowed = CLIENT_EVENTS[name]
    for key, value in props.items():
        if key not in allowed:
            return None, f"Unknown prop {key}."
        if not isinstance(value, str) or value not in allowed[key]:
            return None, f"Invalid value for {key}."
    for required in REQUIRED_PROPS.get(name, ()):
        if required not in props:
            return None, f"Missing prop {required}."
    return name, props
