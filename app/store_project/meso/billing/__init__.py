"""Meso billing (multi-coach SaaS — decision S6).

The coach-pays subscription layer. Phase 1 ships ``access`` (the gating
accessors) over the ``CoachSubscription`` model; Phase 2 adds the Stripe
Checkout/Portal wiring + a clean ``webhooks`` handler here. See
``docs/meso/billing-plan.md``.
"""
