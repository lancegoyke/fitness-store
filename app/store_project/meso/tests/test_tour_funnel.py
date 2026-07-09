"""Issue #441 P3-6 — the staff-gated TourEvent funnel dashboard.

Mirrors the agent usage dashboard's staff gate (anon → login, non-staff → 403,
staff → 200) and pins the presenter's aggregation contract: per-kind totals
(every ``Kind`` value 0-filled), the per-variant breakdown (both variants
present, each kind 0-filled), the per-step advance counts (ordered by the tour
``STEPS`` order), and the total. There's both a rendered-view context test and a
direct ``presenters.tour_funnel`` unit test.

Pre-implementation this is RED: ``meso:tour_funnel`` has no URL/view yet (the
gate + context tests fail with ``NoReverseMatch``) and ``presenters`` has no
``tour_funnel`` (the presenter unit test fails with ``AttributeError``).
"""

import pytest
from django.urls import reverse

from store_project.meso import tour
from store_project.meso.models import TourEvent
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _url():
    # Called inside each test (not at module scope) so a missing route fails
    # the individual test rather than erroring collection of the whole module.
    return reverse("meso:tour_funnel")


def _event(kind, *, variant, step_key="", segment=""):
    return TourEvent.objects.create(
        kind=kind, variant=variant, step_key=step_key, segment=segment
    )


def _seed_events():
    """A fixed mix across both variants / several kinds / several advance steps.

    sandbox: started, advanced×2 (designer), advanced (deliver), opt_in, completed
    self:    started, advanced (agent), dismissed, skipped
    """
    sb = TourEvent.Variant.SANDBOX
    sf = TourEvent.Variant.SELF
    _event(TourEvent.Kind.STARTED, variant=sb, step_key="welcome")
    _event(TourEvent.Kind.ADVANCED, variant=sb, step_key="designer")
    _event(TourEvent.Kind.ADVANCED, variant=sb, step_key="designer")
    _event(TourEvent.Kind.ADVANCED, variant=sb, step_key="deliver")
    _event(TourEvent.Kind.OPT_IN, variant=sb, step_key="welcome", segment="athletes")
    _event(TourEvent.Kind.COMPLETED, variant=sb, step_key="finish")
    _event(TourEvent.Kind.STARTED, variant=sf, step_key="welcome")
    _event(TourEvent.Kind.ADVANCED, variant=sf, step_key="agent")
    _event(TourEvent.Kind.DISMISSED, variant=sf, step_key="results")
    _event(TourEvent.Kind.SKIPPED, variant=sf, step_key="profile")


# Expected aggregation for ``_seed_events`` (kind → count), every Kind 0-filled.
EXPECTED_EVENT_COUNTS = {
    "started": 2,
    "advanced": 4,
    "opt_in": 1,
    "dismissed": 1,
    "completed": 1,
    "skipped": 1,
}
EXPECTED_BY_VARIANT = {
    "sandbox": {
        "started": 1,
        "advanced": 3,
        "opt_in": 1,
        "dismissed": 0,
        "completed": 1,
        "skipped": 0,
    },
    "self": {
        "started": 1,
        "advanced": 1,
        "opt_in": 0,
        "dismissed": 1,
        "completed": 0,
        "skipped": 1,
    },
}
# ADVANCED events, ordered by the tour STEP order.
EXPECTED_STEP_ADVANCES = [("designer", 2), ("deliver", 1), ("agent", 1)]
EXPECTED_TOTAL = 10


def _nonzero_advances(step_advances):
    """The non-zero advance rows as (key, count), preserving order.

    Robust to whichever the impl chose per the spec's "including 0-count steps
    is optional" — the meaningful data + its STEP-order ordering are asserted
    either way.
    """
    return [(d["step_key"], d["count"]) for d in step_advances if d["count"]]


# ---------------------------------------------------------------------------
# staff gate (mirrors UsageDashboardView / test_agent_usage_dashboard)
# ---------------------------------------------------------------------------


class TestTourFunnelGate:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.get(_url())
        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]

    def test_authenticated_non_staff_is_forbidden(self, client):
        client.force_login(UserFactory())
        resp = client.get(_url())
        assert resp.status_code == 403

    def test_staff_gets_the_dashboard(self, client):
        client.force_login(UserFactory(is_staff=True))
        resp = client.get(_url())
        assert resp.status_code == 200
        assert resp.templates[0].name == "meso/tour_funnel.html"


# ---------------------------------------------------------------------------
# the view: aggregation context contract
# ---------------------------------------------------------------------------


class TestTourFunnelContext:
    def test_context_aggregates_the_constructed_events(self, client):
        _seed_events()
        client.force_login(UserFactory(is_staff=True))

        ctx = client.get(_url()).context

        assert ctx["event_counts"] == EXPECTED_EVENT_COUNTS
        assert ctx["by_variant"] == EXPECTED_BY_VARIANT
        assert _nonzero_advances(ctx["step_advances"]) == EXPECTED_STEP_ADVANCES
        assert ctx["total_events"] == EXPECTED_TOTAL


# ---------------------------------------------------------------------------
# the presenter, directly
# ---------------------------------------------------------------------------


class TestTourFunnelPresenter:
    def test_aggregation(self):
        from store_project.meso import presenters

        _seed_events()

        result = presenters.tour_funnel()

        assert result["event_counts"] == EXPECTED_EVENT_COUNTS
        assert result["by_variant"] == EXPECTED_BY_VARIANT
        assert _nonzero_advances(result["step_advances"]) == EXPECTED_STEP_ADVANCES
        assert result["total_events"] == EXPECTED_TOTAL

    def test_funnel_uses_raw_event_counts_with_pct_clamped_to_100(self):
        """Raw event counts (null-coach-safe) with the conversion bar capped.

        Codex review: a distinct-coach funnel would drop reaped sandbox rows
        (coach → NULL on expiry). So the funnel counts raw events — which means
        one sandbox tour's several opt-in rows can exceed starts — and clamps the
        displayed conversion to <= 100%. These events carry no coach (as reaped
        rows don't), proving null-coach rows are still counted.
        """
        from store_project.meso import presenters

        sb = TourEvent.Variant.SANDBOX
        _event(TourEvent.Kind.STARTED, variant=sb)
        for seg in ("athletes", "program", "delivery", "log", "group"):
            _event(TourEvent.Kind.OPT_IN, variant=sb, segment=seg)
        _event(TourEvent.Kind.COMPLETED, variant=sb)

        funnel = {s["label"]: s for s in presenters.tour_funnel()["funnel"]}

        assert funnel["Started"]["count"] == 1
        assert funnel["Opt-in"]["count"] == 5  # raw events, not deduped
        assert funnel["Opt-in"]["pct"] == 100  # 5/1 → 500 → clamped to 100
        assert funnel["Completed"]["count"] == 1
        assert all(0 <= s["pct"] <= 100 for s in funnel.values())

    def test_step_advances_are_ordered_by_the_tour_step_order(self):
        """Advances recorded out of STEP order still come back in STEP order."""
        from store_project.meso import presenters

        sf = TourEvent.Variant.SELF
        # Recorded agent-first, then designer — the presenter must re-order.
        _event(TourEvent.Kind.ADVANCED, variant=sf, step_key="agent")
        _event(TourEvent.Kind.ADVANCED, variant=sf, step_key="designer")

        keys = [
            d["step_key"]
            for d in presenters.tour_funnel()["step_advances"]
            if d["count"]
        ]
        step_order = [s["key"] for s in tour.STEPS]
        assert keys == sorted(keys, key=step_order.index)
        assert keys.index("designer") < keys.index("agent")
