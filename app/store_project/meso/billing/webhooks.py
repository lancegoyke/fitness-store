"""A clean, idempotent Stripe billing webhook handler (S6 billing, Phase 2, D9).

The store already has a products webhook (``payments.views.stripe_webhook``) for
one-time payments; it ignores subscription checkouts (#545). We handle the
*subscription* lifecycle here, on a separate endpoint with its own signing
secret (``MESO_STRIPE_WEBHOOK_SECRET``).

Stripe is the source of truth; this handler mirrors a coach's subscription state
into the local ``CoachSubscription`` so a request can gate without calling Stripe
(D8). It is **idempotent** — keyed by the coach (1:1), driven off the full
subscription object — so a replayed or out-of-order event converges to the same
row. Concurrent deliveries for one coach are serialized on the mirror row
(``_lock_mirror``, #546), so two of them can't both read it, and the ledger,
before either has written. Events handled:

- ``customer.subscription.created|updated`` — upsert from the subscription object
  (status, the subscription item id, period end, and — when Stripe sends one —
  ``trial_end``). The flat Pro plan (D14) reports a single line item, recorded as
  ``stripe_item_id``.
- ``customer.subscription.deleted`` — the subscription is gone → ``canceled``
  (which gates identically to ``free``; the coach keeps read access, D6).
- ``invoice.payment_failed`` / ``invoice.paid`` — a belt-and-suspenders status
  nudge (past_due / active) keyed off the subscription id.

A coach who clicks Subscribe *during* their local no-card trial gets a **Stripe
trial** instead of an immediate charge (#555): Checkout is given
``subscription_data.trial_end`` = the local clock, so the subscription this
webhook sees arrives ``created`` with Stripe status ``trialing`` — which still
maps to local ``TRIALING``, but now carries a ``stripe_subscription_id``. See
``CoachSubscription.is_stripe_trial`` / ``has_live_stripe_subscription`` and
``docs/meso/billing-plan.md``.

A coach we can't resolve (unknown Stripe customer) is logged and ignored — the
event isn't transient, so we don't want Stripe to retry it forever.
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.analytics.track import track
from store_project.meso.models import CoachSubscription

logger = logging.getLogger(__name__)
User = get_user_model()

#: Stripe subscription status → local status. Our *ordinary* trial is
#: local/no-card, so a coach who subscribes straight from free is created
#: ``active``. A coach who instead subscribes mid-trial gets a Stripe trial
#: (#555): Checkout was given ``subscription_data.trial_end``, so Stripe creates
#: the subscription ``trialing`` and this maps it to local ``TRIALING`` too —
#: ``CoachSubscription.is_stripe_trial`` (a ``stripe_subscription_id`` is set)
#: is what tells that apart from the local no-card trial. The rest map
#: defensively.
_STATUS_MAP = {
    "active": CoachSubscription.Status.ACTIVE,
    "trialing": CoachSubscription.Status.TRIALING,
    "past_due": CoachSubscription.Status.PAST_DUE,
    "unpaid": CoachSubscription.Status.PAST_DUE,
    "incomplete": CoachSubscription.Status.PAST_DUE,
    "canceled": CoachSubscription.Status.CANCELED,
    "incomplete_expired": CoachSubscription.Status.CANCELED,
}


def construct_event(payload, sig_header):
    """Verify a raw webhook against the billing endpoint secret → a Stripe event.

    **Fails closed** when the secret is unset: Stripe's verifier would otherwise
    check the signature against ``""``, which an attacker can HMAC-sign trivially —
    so in the dormant pre-configuration deploy state a forged event could drive
    subscription state. With no secret we reject before verifying.

    Raises ``ValueError`` (no secret / bad payload) or
    ``stripe.error.SignatureVerificationError`` (bad signature); the view turns
    any of these into a 400.
    """
    secret = settings.MESO_STRIPE_WEBHOOK_SECRET
    if not secret:
        raise ValueError("Billing webhook secret is not configured.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def handle_event(event):
    """Apply a verified billing event to the local mirror (idempotent)."""
    # stripe 15's ``StripeObject`` is no longer a ``dict`` (no ``.get``, #543):
    # flatten the verified event to plain dicts once, so everything below reads
    # dicts whichever way the event was built.
    if isinstance(event, stripe.StripeObject):
        event = event.to_dict()
    event_type = event["type"]
    obj = event["data"]["object"]
    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _sync_from_subscription(obj, deleted=event_type.endswith("deleted"))
    elif event_type == "invoice.payment_failed":
        # A live subscription's payment just failed → past_due (never a dead
        # one). ``TRIALING`` is included alongside ``ACTIVE`` (#555): a Stripe
        # trial (a trialing row with a stripe_subscription_id) ends with an
        # invoice, and a failed one there means the same thing — past_due. A
        # local no-card trial has no stripe_subscription_id, so it can never
        # match (``_nudge_status`` is keyed by subscription id) and is untouched.
        _nudge_status(
            obj,
            from_statuses=(
                CoachSubscription.Status.ACTIVE,
                CoachSubscription.Status.TRIALING,
            ),
            to_status=CoachSubscription.Status.PAST_DUE,
        )
    elif event_type == "invoice.paid":
        # A past_due subscription recovered → active. Constrained to past_due so a
        # retried/late invoice.paid can't resurrect a canceled subscription.
        _nudge_status(
            obj,
            from_statuses=(CoachSubscription.Status.PAST_DUE,),
            to_status=CoachSubscription.Status.ACTIVE,
        )
    # Anything else is intentionally ignored.


def _ts_to_dt(ts):
    """A Stripe unix timestamp → an aware datetime (None passes through)."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=dt_timezone.utc)


def _coach_for_customer(customer_id):
    """The local coach behind a Stripe customer id, or None (logged) if unknown."""
    if not customer_id:
        return None
    coach = User.objects.filter(stripe_customer_id=customer_id).first()
    if coach is None:
        logger.warning("Billing webhook: no user for Stripe customer %s", customer_id)
    return coach


def _recorded(name, sub_id):
    """Whether a ``name`` event was already written for this Stripe subscription.

    The ``Event`` table doubles as the ledger of which Stripe subscriptions
    have started/cancelled (#509 review) — the mirror's status alone can't
    tell "started" from "recovered from incomplete" apart (an ``incomplete``
    subscription reads ``past_due`` locally, never ``already_live``), and it
    can't tell a subscription that was never live from one that was without
    remembering whether a ``subscription_started`` was ever written.

    The ledger is only as complete as ``track()``, which drops a failed insert
    after logging it: a start that failed to record also suppresses that
    subscription's later cancel from ``past_due``.

    The check and the ``track()`` insert after it are only atomic because
    every caller holds the mirror row lock (``_lock_mirror``, #546).
    """
    return Event.objects.filter(name=name, props__subscription=sub_id).exists()


def _lock_mirror(coach):
    """Lock and return the coach's ``CoachSubscription`` (None if there's none yet).

    Serializes concurrent deliveries for one coach (#546): a second delivery
    waits here until the first commits, then reads the mirror and the ledger it
    left. Without it, both read the old mirror, both pass the ledger check and
    both write ``subscription_started``, or a stale event passes the takeover
    guard in ``_sync_from_subscription`` and overwrites the subscription a concurrent delivery just took
    over. With no mirror row yet there's nothing to lock, so lock the coach's
    user row instead and re-read: a concurrent first delivery holds that lock
    until its new row has committed.
    """
    existing = CoachSubscription.objects.select_for_update().filter(coach=coach).first()
    if existing is None:
        User.objects.select_for_update().filter(pk=coach.pk).first()
        existing = (
            CoachSubscription.objects.select_for_update().filter(coach=coach).first()
        )
    return existing


@transaction.atomic
def _sync_from_subscription(sub_obj, *, deleted):
    """Upsert the coach's ``CoachSubscription`` from a Stripe subscription object."""
    coach = _coach_for_customer(sub_obj.get("customer"))
    if coach is None:
        return
    incoming_id = sub_obj.get("id", "")
    if deleted:
        status = CoachSubscription.Status.CANCELED
    else:
        status = _STATUS_MAP.get(
            sub_obj.get("status"), CoachSubscription.Status.PAST_DUE
        )
    # The mirror is keyed by coach (1:1), so an out-of-order / retried event for a
    # *different* subscription than the one we track must not clobber the current
    # one. A different subscription id only takes over when the existing row is
    # **not already a current Stripe subscription** (e.g. the coach canceled, then
    # re-subscribed) *and* the incoming event is itself live — so neither a stale
    # event for an old id (any status, including a retried ``active``) nor a dead
    # incoming event can replace the current subscription. ``past_due`` counts as
    # current here: it's the real subscription with a failed payment, not replaced.
    # So does a **Stripe trial** (#555, ``has_live_stripe_subscription``): plain
    # ``LIVE_STRIPE_STATUSES`` doesn't include ``trialing``, so without this a
    # stale event for another id could clobber a coach who just subscribed
    # mid-trial and hasn't been charged yet.
    existing = _lock_mirror(coach)
    # A canceled subscription id is terminal (adversarial review, #555): Stripe
    # never reactivates a canceled subscription (``canceled``/``incomplete_expired``
    # are terminal states), so a non-delete event for the SAME id as an
    # already-CANCELED row is stale by definition — a late/retried created or
    # updated must not reopen it into a permanent (never-lapsing) TRIALING/ACTIVE
    # row. A duplicate ``deleted`` still falls through to the idempotent upsert.
    if (
        not deleted
        and existing
        and existing.status == CoachSubscription.Status.CANCELED
        and existing.stripe_subscription_id == incoming_id
    ):
        logger.info(
            "Billing webhook: ignoring stale event for canceled subscription %s",
            incoming_id,
        )
        # Delivery order isn't guaranteed (#555 round 2): a ``deleted`` can
        # arrive BEFORE the ``created``/``updated`` it logically follows, so
        # the row above can go CANCELED without ever recording that the
        # subscription started (nothing was ``already_live`` or previously
        # recorded). This ignored event's own status can show it was live —
        # backfill the missing pair rather than losing it forever. Same
        # savepoint pattern as the ordinary analytics call: it must never
        # fail the webhook, and the mirror stays CANCELED either way.
        try:
            with transaction.atomic():
                _backfill_missed_live_pair(
                    coach, existing, sub_obj, incoming_id, status
                )
        except Exception:
            logger.exception(
                "Billing webhook: backfill analytics failed for subscription %s",
                incoming_id,
            )
        return
    # A stale ``trialing`` event past its own trial_end (adversarial review,
    # #555): Stripe moves a subscription out of ``trialing`` at ``trial_end``, so
    # a late/retried created or updated reporting a ``trialing`` status whose
    # trial_end has already passed can't be describing the subscription's
    # current state — it would otherwise flip an already-charged coach back to
    # "first charge on <past date>" and misreport them as unpaid.
    incoming_trial_end_ts = sub_obj.get("trial_end")
    if (
        not deleted
        and sub_obj.get("status") == "trialing"
        and incoming_trial_end_ts
        and _ts_to_dt(incoming_trial_end_ts) <= timezone.now()
    ):
        logger.info(
            "Billing webhook: ignoring stale trialing event for subscription %s "
            "(trial_end already passed)",
            incoming_id,
        )
        return
    incoming_live = status in CoachSubscription.ACTIVE_STATUSES
    existing_current = existing and existing.has_live_stripe_subscription
    if (
        existing
        and existing.stripe_subscription_id
        and existing.stripe_subscription_id != incoming_id
        and (existing_current or not incoming_live)
    ):
        logger.info(
            "Billing webhook: ignoring event for subscription %s "
            "(coach tracks %s, status=%s)",
            incoming_id,
            existing.stripe_subscription_id,
            existing.status,
        )
        return
    # The flat Pro plan (D14) is a single line item; record its id. A legacy
    # base+per-seat subscription (pre-flat) reports two — take the first item's id
    # so an old sub still maps to something valid (the id is only used to detect a
    # stale event for a *different* subscription, never to resize a quantity).
    items = (sub_obj.get("items") or {}).get("data") or [{}]
    item = items[0]
    # subscription_started/cancelled analytics (#509): read off ``existing``
    # BEFORE the upsert below overwrites the mirror.
    previous_status = existing.status if existing else ""
    already_live = (
        existing is not None
        and existing.stripe_subscription_id == incoming_id
        and existing.status in CoachSubscription.ACTIVE_STATUSES
    )
    defaults = {
        "status": status,
        "stripe_subscription_id": sub_obj.get("id", ""),
        "stripe_item_id": item.get("id", ""),
        "current_period_end": _ts_to_dt(sub_obj.get("current_period_end")),
    }
    # Copy Stripe's trial_end onto the row when it sends one (a Stripe trial,
    # #555) — it may differ from the local value Checkout was given (clock
    # skew / Stripe's own rounding), so Stripe's is authoritative once it's
    # tracking the trial. When Stripe has no trial_end (a plain, non-trial
    # subscription, or any later event on one) the local value is left alone:
    # it's the single-use trial marker (``start_trial`` sets it once) and feeds
    # the "trial started" read in the product-analytics dashboard (#509).
    incoming_trial_end = sub_obj.get("trial_end")
    if incoming_trial_end:
        defaults["trial_end"] = _ts_to_dt(incoming_trial_end)
    sub, _created = CoachSubscription.objects.update_or_create(
        coach=coach, defaults=defaults
    )
    # Analytics run AFTER the mirror write above and can't fail it: the mirror
    # is what this webhook exists for. The savepoint matters on PostgreSQL,
    # where a failed query aborts the whole transaction even once caught.
    try:
        with transaction.atomic():
            _track_subscription_change(
                coach, sub, sub_obj, status, previous_status, already_live, existing
            )
    except Exception:
        logger.exception(
            "Billing webhook: analytics failed for subscription %s", incoming_id
        )


def _backfill_missed_live_pair(coach, existing, sub_obj, sub_id, mapped_status):
    """Reconstruct a missed ``subscription_started``/``cancelled`` pair (#555 round 2).

    Called from the canceled-id guard, for an event it's about to ignore.
    ``existing`` is the row's current (CANCELED) state; ``mapped_status`` is
    the ignored event's own mapped status. A no-op unless that status shows
    the subscription really was live and no ``subscription_started`` was
    ever recorded for this id — #509's ledger rule ("started once, cancelled
    at most once, whatever the delivery order") still has to hold even when
    a ``deleted`` beats its own ``created``/``updated`` to the handler.
    """
    if mapped_status not in CoachSubscription.ACTIVE_STATUSES:
        return
    if _recorded(EventName.SUBSCRIPTION_STARTED, sub_id):
        return
    track(
        EventName.SUBSCRIPTION_STARTED,
        actor=coach,
        subject=existing,
        via="stripe",
        subscription=sub_id,
        status=mapped_status,
        previous=existing.status,
    )
    if not _recorded(EventName.SUBSCRIPTION_CANCELLED, sub_id):
        track(
            EventName.SUBSCRIPTION_CANCELLED,
            actor=coach,
            subject=existing,
            via="stripe",
            subscription=sub_id,
            status=CoachSubscription.Status.CANCELED,
            previous=mapped_status,
            reason=(sub_obj.get("cancellation_details") or {}).get("reason") or "",
        )


def _track_subscription_change(
    coach, sub, sub_obj, status, previous_status, already_live, existing
):
    """Record ``subscription_started``/``cancelled`` for one mirrored Stripe event."""
    sub_id = sub_obj.get("id", "")
    # ``already_live`` alone isn't enough to tell "started" from "recovered":
    # Stripe can deliver created(incomplete) → invoice.paid → updated(active),
    # where ``_nudge_status`` flips past_due→active before this
    # ``updated(active)`` arrives — so ``already_live`` would see a live mirror
    # and skip the real first "started". The ledger (``_recorded``) is what
    # remembers a started was written.
    if (
        status in CoachSubscription.ACTIVE_STATUSES
        and not already_live
        and not _recorded(EventName.SUBSCRIPTION_STARTED, sub_id)
    ):
        track(
            EventName.SUBSCRIPTION_STARTED,
            actor=coach,
            subject=sub,
            via="stripe",
            subscription=sub_id,
            status=status,
            previous=previous_status,
        )
    elif (
        status == CoachSubscription.Status.CANCELED
        and existing is not None
        and existing.status != CoachSubscription.Status.CANCELED
        and (already_live or _recorded(EventName.SUBSCRIPTION_STARTED, sub_id))
        and not _recorded(EventName.SUBSCRIPTION_CANCELLED, sub_id)
    ):
        track(
            EventName.SUBSCRIPTION_CANCELLED,
            actor=coach,
            subject=sub,
            via="stripe",
            subscription=sub_id,
            status=status,
            previous=previous_status,
            reason=(sub_obj.get("cancellation_details") or {}).get("reason") or "",
        )


@transaction.atomic
def _nudge_status(invoice_obj, *, from_statuses, to_status):
    """A constrained status nudge from an invoice event, keyed by the subscription id.

    The authoritative state comes from the subscription events; this just keeps the
    mirror fresh between them. It is deliberately a **single guarded transition**
    (one of ``from_statuses`` → ``to_status``): an invoice event only flips a row
    already in an expected source state, so a retried/out-of-order invoice can
    never resurrect a ``canceled`` subscription or otherwise jump the state
    machine. A no-match (wrong state, or no mirror yet) is a harmless no-op.
    ``payment_failed`` guards ``(active, trialing)`` so a Stripe trial (#555) can
    land ``past_due`` too; ``paid`` guards ``(past_due,)`` only.
    """
    sub_id = invoice_obj.get("subscription")
    if not sub_id:
        return
    # The same mirror row lock as ``_lock_mirror``, taken before the ledger
    # check in ``_track_invoice_start`` (#546). Also captures the pre-nudge
    # status as ``previous`` for that analytics event below.
    locked = (
        CoachSubscription.objects.select_for_update()
        .filter(stripe_subscription_id=sub_id)
        .first()
    )
    previous = locked.status if locked else ""
    updated = CoachSubscription.objects.filter(
        stripe_subscription_id=sub_id, status__in=from_statuses
    ).update(status=to_status)
    if not updated:
        logger.info(
            "Billing webhook: no %s mirror for subscription %s (invoice)",
            from_statuses,
            sub_id,
        )
        return
    if to_status == CoachSubscription.Status.ACTIVE:
        try:
            with transaction.atomic():
                _track_invoice_start(sub_id, previous)
        except Exception:
            logger.exception(
                "Billing webhook: analytics failed for subscription %s", sub_id
            )


def _track_invoice_start(sub_id, previous):
    """Record ``subscription_started`` when an invoice nudge made it live."""
    if not _recorded(EventName.SUBSCRIPTION_STARTED, sub_id):
        # A past_due→active recovery (e.g. created(incomplete) → invoice.paid)
        # is a real first "started" the subscription-object events never see
        # (an ``incomplete`` subscription never reads ``already_live``, and
        # ``_nudge_status``'s bare ``.update()`` writes no event). No symmetric
        # event on the active→past_due nudge — that's not a cancellation.
        row = (
            CoachSubscription.objects.filter(stripe_subscription_id=sub_id)
            .select_related("coach")
            .first()
        )
        if row is not None:
            track(
                EventName.SUBSCRIPTION_STARTED,
                actor=row.coach,
                subject=row,
                via="stripe",
                subscription=sub_id,
                status=CoachSubscription.Status.ACTIVE,
                previous=previous,
            )
