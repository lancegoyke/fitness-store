"""Guided demo onboarding tour — Phase 1: segmenting the demo loaders.

``load_demo`` (``meso/demo.py``) used to be one monolithic load. This phase
splits it into idempotent, per-coach **segment** loaders — ``athletes`` /
``program`` / ``delivery`` / ``log`` — that each ensure their own
prerequisites, so a later guided tour (Phase 2) can offer them one at a time,
in any order, from their own step. This is a **behavior-preserving refactor**
(``docs/meso/demo-onboarding-tour-plan.md``, Phase 1): ``load_demo`` stays the
aggregate and ``create_sandbox`` still eager-loads it, so nothing changes for
users yet — these tests pin that the split reproduces today's exact workspace.

Covers:

- each segment loader, run alone on an empty workspace, creates *exactly* its
  slice plus its prerequisites — no more, no less (the ``has_*`` predicates
  make this checkable);
- each loader is idempotent;
- loaders compose safely out of dependency order;
- all segments together reproduce ``load_demo``'s workspace exactly;
- the ``has_*`` predicates flip precisely when their segment loads;
- ``clear_demo`` still removes everything after a piecemeal load;
- the ``demo_load`` endpoint's new optional ``segment`` POST field;
- no outbound email from any segment load (mirrors ``test_demo.py``'s
  no-notifications guarantee).
"""

import pytest
from django.urls import reverse

from store_project.meso import demo
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import Week
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


def _coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


def _maya_plan(coach):
    """Maya's individual plan."""
    return Plan.objects.get(
        relationship__coach=coach,
        relationship__is_demo=True,
    )


def _maya_week(coach):
    """The ``Week`` ``SAMPLE_LOG`` describes, on Maya's individual plan."""
    return Week.objects.get(
        mesocycle__plan__relationship__coach=coach,
        mesocycle__plan__relationship__is_demo=True,
        mesocycle__name=demo.SAMPLE_LOG["mesocycle"],
        index=demo.SAMPLE_LOG["week_index"],
    )


def _row_counts():
    """A cheap fingerprint of every model the demo loaders touch."""
    return (
        User.objects.count(),
        CoachAthlete.objects.count(),
        Plan.objects.count(),
        Mesocycle.objects.count(),
        Week.objects.count(),
        SessionLog.objects.count(),
        LoggedSet.objects.count(),
    )


# ---------------------------------------------------------------------------
# Each segment, alone on an empty workspace, creates exactly its slice
# ---------------------------------------------------------------------------


class TestSegmentSlices:
    def test_load_athletes_creates_only_athletes(self):
        coach = _coach()
        demo.load_athletes(coach)
        assert demo.has_athletes(coach) is True
        assert demo.has_program(coach) is False
        assert demo.has_delivery(coach) is False
        assert demo.has_log(coach) is False
        assert CoachAthlete.objects.for_coach(coach).filter(is_demo=True).count() == 5

    def test_load_program_creates_athletes_and_plan_tree_only(self):
        coach = _coach()
        demo.load_program(coach)
        assert demo.has_athletes(coach) is True
        assert demo.has_program(coach) is True
        assert demo.has_delivery(coach) is False
        assert demo.has_log(coach) is False
        plan = _maya_plan(coach)
        assert plan.title == demo.SAMPLE_PLAN["title"]
        assert plan.mesocycles.exists()

    def test_load_delivery_creates_program_and_delivered_week_only(self):
        coach = _coach()
        demo.load_delivery(coach)
        assert demo.has_program(coach) is True
        assert demo.has_delivery(coach) is True
        assert demo.has_log(coach) is False
        week = _maya_week(coach)
        assert week.delivered_at is not None
        assert not SessionLog.objects.filter(
            athlete__in=demo._demo_athletes(coach)
        ).exists()

    def test_load_log_creates_delivery_and_log_only(self):
        coach = _coach()
        demo.load_log(coach)
        assert demo.has_delivery(coach) is True
        assert demo.has_log(coach) is True
        log = SessionLog.objects.get(athlete__in=demo._demo_athletes(coach))
        assert log.sets.exists()


# ---------------------------------------------------------------------------
# Idempotency — running a segment twice never duplicates
# ---------------------------------------------------------------------------


class TestSegmentIdempotency:
    @pytest.mark.parametrize("segment", list(demo.SEGMENTS))
    def test_segment_loader_is_idempotent(self, segment):
        coach = _coach()
        loader = demo.SEGMENTS[segment]
        loader(coach)
        first = _row_counts()
        loader(coach)
        assert _row_counts() == first


# ---------------------------------------------------------------------------
# Out-of-order composition — steps can be taken in any order
# ---------------------------------------------------------------------------


class TestOutOfOrderComposition:
    def test_all_segments_in_reverse_dependency_order(self):
        """Loading every segment back-to-front still lands on the full workspace."""
        coach = _coach()
        for name in reversed(list(demo.SEGMENTS)):
            demo.SEGMENTS[name](coach)
        assert demo.has_athletes(coach) is True
        assert demo.has_program(coach) is True
        assert demo.has_delivery(coach) is True
        assert demo.has_log(coach) is True
        # No duplicate rows from the repeated prerequisite chains.
        assert CoachAthlete.objects.for_coach(coach).filter(is_demo=True).count() == 5
        assert (
            Plan.objects.filter(
                relationship__coach=coach,
                relationship__is_demo=True,
            ).count()
            == 1
        )


# ---------------------------------------------------------------------------
# Equivalence — all segments together reproduce load_demo exactly
# ---------------------------------------------------------------------------


class TestSegmentsEquivalentToLoadDemo:
    def test_all_segments_match_load_demo(self):
        coach_a = _coach()
        for loader in demo.SEGMENTS.values():
            loader(coach_a)
        coach_b = _coach()
        demo.load_demo(coach_b)

        def slugs(coach):
            return {u.email.split("@")[0] for u in demo._demo_athletes(coach)}

        expected_slugs = {spec["slug"] for spec in demo.ATHLETES}
        assert slugs(coach_a) == slugs(coach_b) == expected_slugs

        plan_a, plan_b = _maya_plan(coach_a), _maya_plan(coach_b)
        assert plan_a.title == plan_b.title == demo.SAMPLE_PLAN["title"]
        assert plan_a.mesocycles.count() == plan_b.mesocycles.count()

        week_a, week_b = _maya_week(coach_a), _maya_week(coach_b)
        assert week_a.delivered_at is not None
        assert week_b.delivered_at is not None

        def logged_sets(coach):
            return LoggedSet.objects.filter(
                session_log__athlete__in=demo._demo_athletes(coach)
            ).count()

        assert (
            SessionLog.objects.filter(athlete__in=demo._demo_athletes(coach_a)).count()
            == 1
        )
        assert (
            SessionLog.objects.filter(athlete__in=demo._demo_athletes(coach_b)).count()
            == 1
        )
        assert logged_sets(coach_a) == logged_sets(coach_b) > 0


# ---------------------------------------------------------------------------
# has_* predicates — derived from data, flip exactly when their segment loads
# ---------------------------------------------------------------------------


class TestHasPredicates:
    def test_predicates_flip_one_at_a_time_as_segments_load(self):
        coach = _coach()
        assert demo.has_athletes(coach) is False
        assert demo.has_program(coach) is False
        assert demo.has_delivery(coach) is False
        assert demo.has_log(coach) is False

        demo.load_athletes(coach)
        assert demo.has_athletes(coach) is True
        assert demo.has_program(coach) is False

        demo.load_program(coach)
        assert demo.has_program(coach) is True
        assert demo.has_delivery(coach) is False

        demo.load_delivery(coach)
        assert demo.has_delivery(coach) is True
        assert demo.has_log(coach) is False

        demo.load_log(coach)
        assert demo.has_log(coach) is True

    def test_has_demo_mirrors_has_athletes(self):
        coach = _coach()
        assert demo.has_demo(coach) is False
        demo.load_athletes(coach)
        assert demo.has_demo(coach) is True


# ---------------------------------------------------------------------------
# clear_demo still removes everything after a piecemeal load
# ---------------------------------------------------------------------------


class TestClearAfterPiecemealLoad:
    def test_clear_demo_removes_all_segments(self):
        coach = _coach()
        demo.load_log(coach)
        demo.clear_demo(coach)
        assert demo.has_athletes(coach) is False
        assert demo.has_program(coach) is False
        assert demo.has_delivery(coach) is False
        assert demo.has_log(coach) is False
        assert CoachAthlete.objects.for_coach(coach).filter(is_demo=True).count() == 0
        assert list(demo._demo_athletes(coach)) == []


# ---------------------------------------------------------------------------
# No outbound email from any segment load
# ---------------------------------------------------------------------------


class TestNoNotifications:
    @pytest.mark.parametrize("segment", list(demo.SEGMENTS))
    def test_segment_load_sends_no_email(self, segment, mailoutbox):
        coach = _coach()
        demo.SEGMENTS[segment](coach)
        assert mailoutbox == []


# ---------------------------------------------------------------------------
# The demo_load endpoint's optional ``segment`` field
# ---------------------------------------------------------------------------


class TestDemoLoadEndpointSegments:
    def test_no_segment_behaves_as_today(self, client):
        coach = _coach()
        client.force_login(coach)
        resp = client.post(reverse("meso:demo_load"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(coach) is True
        assert demo.has_program(coach) is True

    def test_valid_segment_loads_only_that_segment(self, client):
        coach = _coach()
        client.force_login(coach)
        resp = client.post(reverse("meso:demo_load"), {"segment": "athletes"})
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_athletes(coach) is True
        assert demo.has_program(coach) is False

    def test_invalid_segment_400s_and_creates_nothing(self, client):
        coach = _coach()
        client.force_login(coach)
        resp = client.post(reverse("meso:demo_load"), {"segment": "bogus"})
        assert resp.status_code == 400
        assert demo.has_demo(coach) is False
        assert not CoachAthlete.objects.filter(is_demo=True).exists()

    def test_segment_load_ensures_a_coach_profile(self, client):
        user = UserFactory()  # no CoachProfile yet
        client.force_login(user)
        client.post(reverse("meso:demo_load"), {"segment": "athletes"})
        assert CoachProfile.objects.filter(user=user).exists()

    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.post(reverse("meso:demo_load"), {"segment": "athletes"})
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url
        assert not CoachAthlete.objects.filter(is_demo=True).exists()

    def test_segment_load_sends_no_email(self, client, mailoutbox):
        coach = _coach()
        client.force_login(coach)
        client.post(reverse("meso:demo_load"), {"segment": "log"})
        assert mailoutbox == []


# ---------------------------------------------------------------------------
# The demo_load endpoint's optional ``next`` field (guided-tour Phase 2):
# the tour's segment forms fire from mid-tour pages (designer, deliver, ...),
# and always redirecting to the roster would teleport the user away from the
# step they're on. Only a safe local path is honored.
# ---------------------------------------------------------------------------


class TestDemoLoadNextRedirect:
    def test_valid_relative_next_is_honored(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:demo_load"),
            {"segment": "delivery", "next": "/meso/deliver/5/"},
        )

        assert resp.status_code == 302
        assert resp.url == "/meso/deliver/5/"
        assert demo.has_delivery(coach) is True

    def test_next_with_a_querystring_is_honored(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:demo_load"),
            {"segment": "athletes", "next": "/meso/deliver/5/?week=9"},
        )

        assert resp.status_code == 302
        assert resp.url == "/meso/deliver/5/?week=9"

    def test_absent_next_falls_back_to_the_roster(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:demo_load"), {"segment": "athletes"})

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    def test_absolute_external_next_is_rejected(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:demo_load"),
            {"segment": "athletes", "next": "https://evil.example/phish"},
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_athletes(coach) is True  # the load itself still ran

    def test_scheme_relative_next_is_rejected(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:demo_load"),
            {"segment": "athletes", "next": "//evil.example/phish"},
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    def test_non_rooted_next_is_rejected(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:demo_load"),
            {"segment": "athletes", "next": "meso/deliver/5/"},
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    def test_aggregate_load_without_next_behaves_exactly_as_today(self, client):
        """The pre-tour callers (roster card, tour_skip) send no ``next``."""
        coach = _coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:demo_load"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(coach) is True

    def test_aggregate_load_honors_a_safe_next_too(self, client):
        """``next`` isn't segment-only — but no existing caller sends it."""
        coach = _coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:demo_load"), {"next": "/meso/designer/"})

        assert resp.status_code == 302
        assert resp.url == "/meso/designer/"
