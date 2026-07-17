"""First-time UX — Phase 2: the coach-scoped one-click demo (Q3).

Phase 1 made individual plan creation real; this phase lets a brand-new coach
**explore a populated workspace** before committing real clients, then remove it.
``meso/demo.py`` wraps the seed's building blocks into ``load_demo`` /
``clear_demo`` / ``has_demo``, scoped to the requesting coach.

The guardrails these tests pin (``docs/archive/meso/first-time-ux-plan.md``, Q3):

- **coach-scoped + collision-free** — each coach's demo athletes are namespaced,
  so two coaches can both load the demo and neither sees the other's data;
- **idempotent + fully removable** — loading twice never duplicates, and
  ``clear_demo`` removes *exactly* the coach's demo (never their real data);
- **billing-neutral** — demo athletes are not billable seats, so loading the
  demo never trips the paywall (``CoachAthlete.billable`` / ``access``);
- **no outbound email/push** — demo athletes are fake people: loading the demo
  notifies nobody, and they carry the delivery-email opt-out.
"""

import pytest
from django.urls import reverse

from store_project.meso import demo
from store_project.meso.billing import access
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
from store_project.meso.models import PushSubscription
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


def _coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


# ---------------------------------------------------------------------------
# load_demo — stands up a populated, scoped workspace
# ---------------------------------------------------------------------------


class TestLoadDemo:
    def test_creates_demo_athletes_scoped_to_coach(self):
        coach = _coach()
        demo.load_demo(coach)
        demo_links = CoachAthlete.objects.for_coach(coach).filter(is_demo=True)
        # The five prototype athletes, all active demo links the coach manages.
        assert demo_links.count() == 5
        assert all(link.is_active for link in demo_links)

    def test_demo_athletes_have_a_built_deliverable_logged_plan(self):
        """Maya gets the sample plan, delivered + logged, so results light up."""
        coach = _coach()
        demo.load_demo(coach)
        plan = Plan.objects.filter(
            relationship__coach=coach, relationship__is_demo=True
        ).first()
        assert plan is not None
        assert plan.status == Plan.Status.ACTIVE
        assert plan.mesocycles.exists()  # a real tree, not a bare scaffold
        # The current week was delivered + logged at the model layer.
        assert SessionLog.objects.filter(
            athlete__in=demo._demo_athletes(coach)
        ).exists()

    def test_demo_athletes_are_namespaced_non_routable_and_opted_out(self):
        coach = _coach()
        demo.load_demo(coach)
        for athlete in demo._demo_athletes(coach):
            # Non-routable address (RFC 6761 .invalid), namespaced by coach.
            assert athlete.email.endswith(".demo.invalid")
            assert coach.pk.hex in athlete.email
            # Belt-and-suspenders: opted out of delivery email regardless.
            profile = AthleteProfile.objects.get(user=athlete)
            assert profile.delivery_email_opt_out is True

    def test_has_demo_reflects_state(self):
        coach = _coach()
        assert demo.has_demo(coach) is False
        demo.load_demo(coach)
        assert demo.has_demo(coach) is True

    def test_idempotent(self):
        coach = _coach()
        demo.load_demo(coach)
        first_users = User.objects.count()
        first_plans = Plan.objects.count()
        demo.load_demo(coach)
        assert User.objects.count() == first_users
        assert Plan.objects.count() == first_plans
        assert CoachAthlete.objects.for_coach(coach).filter(is_demo=True).count() == 5


# ---------------------------------------------------------------------------
# clear_demo — removes exactly the coach's demo, nothing else
# ---------------------------------------------------------------------------


class TestClearDemo:
    def test_removes_all_demo_data(self):
        coach = _coach()
        demo.load_demo(coach)
        demo.clear_demo(coach)
        assert CoachAthlete.objects.for_coach(coach).filter(is_demo=True).count() == 0
        assert list(demo._demo_athletes(coach)) == []
        assert demo.has_demo(coach) is False

    def test_leaves_real_data_untouched(self):
        coach = _coach()
        real_athlete = UserFactory()
        real_link = CoachAthlete.objects.create(
            coach=coach,
            athlete=real_athlete,
            status=CoachAthlete.Status.ACTIVE,
            invited_by=CoachAthlete.InvitedBy.COACH,
        )
        real_plan = real_link.create_plan(title="Real program")
        demo.load_demo(coach)
        demo.clear_demo(coach)
        # The real relationship + plan + user survive intact.
        real_link.refresh_from_db()
        assert real_link.is_active
        assert Plan.objects.filter(pk=real_plan.pk).exists()
        assert User.objects.filter(pk=real_athlete.pk).exists()

    def test_clear_is_safe_when_no_demo(self):
        coach = _coach()
        demo.clear_demo(coach)  # no-op, no error
        assert demo.has_demo(coach) is False


# ---------------------------------------------------------------------------
# Scoping — one coach's demo is invisible to and unaffected by another's
# ---------------------------------------------------------------------------


class TestScoping:
    def test_two_coaches_can_both_load_without_collision(self):
        coach_a = _coach()
        coach_b = _coach()
        demo.load_demo(coach_a)
        demo.load_demo(coach_b)
        assert CoachAthlete.objects.for_coach(coach_a).filter(is_demo=True).count() == 5
        assert CoachAthlete.objects.for_coach(coach_b).filter(is_demo=True).count() == 5
        # Distinct athlete users per coach (namespaced emails, no shared rows).
        a_ids = set(u.pk for u in demo._demo_athletes(coach_a))
        b_ids = set(u.pk for u in demo._demo_athletes(coach_b))
        assert a_ids.isdisjoint(b_ids)

    def test_clear_one_coach_leaves_the_other(self):
        coach_a = _coach()
        coach_b = _coach()
        demo.load_demo(coach_a)
        demo.load_demo(coach_b)
        demo.clear_demo(coach_a)
        assert demo.has_demo(coach_a) is False
        assert demo.has_demo(coach_b) is True
        assert CoachAthlete.objects.for_coach(coach_b).filter(is_demo=True).count() == 5


# ---------------------------------------------------------------------------
# Billing-neutral — demo athletes are not billable seats
# ---------------------------------------------------------------------------


class TestBillingNeutral:
    def test_billable_queryset_excludes_demo(self):
        coach = _coach()
        real_athlete = UserFactory()
        CoachAthlete.objects.create(
            coach=coach,
            athlete=real_athlete,
            status=CoachAthlete.Status.ACTIVE,
            invited_by=CoachAthlete.InvitedBy.COACH,
        )
        demo.load_demo(coach)
        # Six active links (1 real + 5 demo) but only the real one is billable.
        assert CoachAthlete.objects.for_coach(coach).active().count() == 6
        assert CoachAthlete.objects.for_coach(coach).billable().count() == 1

    def test_loading_demo_does_not_trip_the_free_seat_cap(self):
        """A free coach (cap 1) can load the 5-athlete demo without going over."""
        coach = _coach()  # no subscription row → free tier
        demo.load_demo(coach)
        assert access.active_seat_count(coach) == 0
        assert access.is_over_limit(coach) is False
        assert access.can_add_athlete(coach) is True

    def test_demo_links_are_never_suspended(self):
        """An over-limit coach suspends real athletes, never demo ones."""
        coach = _coach()
        # Two real active athletes on a free tier (cap 1) → over limit.
        for _ in range(2):
            CoachAthlete.objects.create(
                coach=coach,
                athlete=UserFactory(),
                status=CoachAthlete.Status.ACTIVE,
                invited_by=CoachAthlete.InvitedBy.COACH,
            )
        demo.load_demo(coach)
        suspended = access.suspended_athlete_ids(coach)
        assert len(suspended) == 1  # one real athlete over the cap
        demo_link_ids = set(
            CoachAthlete.objects.for_coach(coach)
            .filter(is_demo=True)
            .values_list("pk", flat=True)
        )
        assert demo_link_ids.isdisjoint(suspended)


# ---------------------------------------------------------------------------
# No outbound email/push — demo athletes are fake people
# ---------------------------------------------------------------------------


class TestNoNotifications:
    def test_load_sends_no_email(self, mailoutbox):
        coach = _coach()
        demo.load_demo(coach)
        assert mailoutbox == []

    def test_load_creates_no_push_subscriptions(self):
        coach = _coach()
        demo.load_demo(coach)
        assert PushSubscription.objects.count() == 0


# ---------------------------------------------------------------------------
# Views — demo/load + demo/clear endpoints
# ---------------------------------------------------------------------------


class TestDemoViews:
    def test_load_endpoint_creates_demo_and_redirects(self, client):
        coach = _coach()
        client.force_login(coach)
        resp = client.post(reverse("meso:demo_load"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(coach) is True

    def test_clear_endpoint_removes_demo_and_redirects(self, client):
        coach = _coach()
        demo.load_demo(coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:demo_clear"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(coach) is False

    def test_load_endpoint_ensures_a_coach_profile(self, client):
        """Loading the demo makes the user a real coach (has a CoachProfile)."""
        user = UserFactory()  # no profile yet
        client.force_login(user)
        client.post(reverse("meso:demo_load"))
        assert CoachProfile.objects.filter(user=user).exists()

    def test_load_is_post_only(self, client):
        coach = _coach()
        client.force_login(coach)
        assert client.get(reverse("meso:demo_load")).status_code == 405

    def test_clear_is_post_only(self, client):
        coach = _coach()
        client.force_login(coach)
        assert client.get(reverse("meso:demo_clear")).status_code == 405

    def test_load_requires_login(self, client):
        resp = client.post(reverse("meso:demo_load"))
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url
        assert not CoachAthlete.objects.filter(is_demo=True).exists()


# ---------------------------------------------------------------------------
# Roster surface — empty-state onboarding + demo banner
# ---------------------------------------------------------------------------


class TestRosterSurface:
    def test_empty_roster_offers_the_demo(self, client):
        coach = _coach()
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert reverse("meso:demo_load").encode() in resp.content

    def test_roster_with_demo_offers_removal(self, client):
        coach = _coach()
        demo.load_demo(coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert reverse("meso:demo_clear").encode() in resp.content
        # The onboarding "load demo" CTA is gone once demo data is present.
        assert reverse("meso:demo_load").encode() not in resp.content
