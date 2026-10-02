"""First-time UX Phase 4 — athlete first-run polish (athlete surface).

Two affordances make a newly-invited athlete's first run obvious:

- a one-time **first-log coachmark** that teaches the athlete to tap a session
  and log it. It is **server-driven** (shown only while the athlete has a
  delivered session to tap but has *never* logged any session), so it is
  naturally one-time and cross-device — it vanishes the moment the first log
  lands, with no per-device flag or migration;
- a dismissible **PWA install card** on the training home, revealed by
  ``meso_onboarding.js`` (which also persists coachmark dismissals).

The reveal/dismiss logic is browser-side (``beforeinstallprompt`` / standalone
detection / localStorage) and unit-tested in ``frontend/meso_onboarding.test.js``;
what is pinned here is the *server-side contract* — when the coachmark renders
and that the onboarding chrome is wired into the athlete templates only.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(*, athlete=None, coach=None, delivered=True):
    """A plan → week → session → prescription for one athlete.

    ``delivered`` stamps the week (the notify marker — since 2d the athlete's
    home surfaces live sessions either way).
    """
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=2,
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name="Lower", bias="Quad")
    cell = presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return athlete, coach, session, cell


HOME = reverse("meso:athlete_home")
ROSTER = reverse("meso:roster")
ONBOARDING_JS = "js/meso_onboarding.js"

# Markers the templates carry; asserting on these keeps the test resilient to
# copy changes while pinning the behaviour (which coachmark, install card).
HOME_HINT = 'data-coachmark-key="firstlog-home"'
SESSION_HINT = 'data-coachmark-key="firstlog-session"'
INSTALL_CARD = 'id="meso-install-card"'


def session_url(session):
    return reverse("meso:athlete_session", kwargs={"pk": session.pk})


def coachmark(body, key):
    """One coachmark card's markup: from its key to its dismiss button."""
    start = body.index(f'data-coachmark-key="{key}"')
    return body[start : body.index("data-coachmark-dismiss", start)]


class TestFirstLogHintHome:
    """The home coachmark shows only to an athlete who can — but never has — logged."""

    def test_shows_for_new_athlete_with_a_delivered_session(self, client):
        athlete, *_ = seed()
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert HOME_HINT in body

    def test_hidden_once_the_athlete_has_completed_a_log(self, client):
        athlete, _c, session, _p = seed()
        SessionLogFactory(
            session=session, athlete=athlete, status=SessionLog.Status.DONE
        )
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert HOME_HINT not in body

    def test_a_draft_save_keeps_the_hint(self, client):
        # "Save progress" writes a *pending* log while the session still reads
        # "To do" — the athlete hasn't truly logged yet, so keep teaching.
        athlete, _c, session, _p = seed()
        SessionLogFactory(
            session=session, athlete=athlete, status=SessionLog.Status.PENDING
        )
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert HOME_HINT in body

    def test_hidden_when_there_are_no_sessions_yet(self, client):
        # No session to tap (the coach's plan has an empty week) → the "tap a
        # session below" nudge would point at nothing, so it must not show.
        # (2d: delivery no longer gates visibility, so only true emptiness
        # hides the hint.)
        coach = UserFactory()
        athlete = UserFactory()
        rel = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        plan = PlanFactory(
            relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
        )
        meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
        WeekFactory(mesocycle=meso, index=1, delivered_at=None)
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert HOME_HINT not in body

    def test_a_log_on_another_session_counts_as_having_logged(self, client):
        # The hint is about ever-having-logged, not this plan — any prior log
        # (here under a different coach) means the athlete already knows how, so
        # don't re-teach.
        athlete, _coach, _s, _p = seed()
        _a2, _c2, other_session, _p2 = seed(athlete=athlete)
        SessionLogFactory(
            session=other_session, athlete=athlete, status=SessionLog.Status.DONE
        )
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert HOME_HINT not in body


class TestSessionPageHasNoFirstLogCoachmark:
    """The session page teaches logging inline; only the home page keeps the tip.

    The retired Set-row logger needed a coachmark to explain its two entry paths.
    There is one path now, spelled out by the always-visible ``log-instruction``
    line, so the session page no longer renders the ``firstlog-session`` tip
    (the home tip, ``firstlog-home``, is unchanged).
    """

    def test_a_first_time_logger_sees_the_inline_instruction_not_a_coachmark(
        self, client
    ):
        athlete, _c, session, _p = seed()
        client.force_login(athlete)
        body = client.get(session_url(session)).content.decode()
        assert SESSION_HINT not in body
        assert 'data-testid="log-instruction"' in body
        assert "Finish session" in body

    def test_a_pending_draft_does_not_bring_the_session_coachmark_back(self, client):
        athlete, _c, session, _p = seed()
        SessionLogFactory(
            session=session, athlete=athlete, status=SessionLog.Status.PENDING
        )
        client.force_login(athlete)
        body = client.get(session_url(session)).content.decode()
        assert SESSION_HINT not in body


class TestLoggingCopyMatchesThePage:
    """The how-to-log copy describes the page as it is: type lines, then finish."""

    def test_header_counter_counts_logged_sets(self, client):
        # The counter is the server's single count of logged sets (typed lines
        # tick it), so it reads "N of M sets logged" -- not "checked off".
        athlete, _c, session, _p = seed()
        client.force_login(athlete)
        body = client.get(session_url(session)).content.decode()
        assert "sets logged" in body
        assert "sets checked off" not in body

    def test_home_coachmark_says_type_then_finish(self, client):
        athlete, *_ = seed()
        client.force_login(athlete)
        hint = coachmark(client.get(HOME).content.decode(), "firstlog-home")
        assert "check off" not in hint
        assert "Finish session" in hint
        assert "Log session" not in hint

    def test_session_page_leaks_no_template_comment(self, client):
        # A multi-line {# #} renders as visible text (5a UAT found two here).
        athlete, _c, session, _p = seed()
        client.force_login(athlete)
        body = client.get(session_url(session)).content.decode()
        assert "{#" not in body
        assert "#}" not in body


class TestOnboardingWiring:
    """The install card + onboarding JS belong to the athlete surface only."""

    def test_home_renders_install_card_and_onboarding_js(self, client):
        athlete, *_ = seed()
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert INSTALL_CARD in body
        assert ONBOARDING_JS in body

    def test_session_loads_onboarding_js_without_an_install_card(self, client):
        # The install prompt lives on the training home; the session page still
        # gets the onboarding JS (it powers the first-log coachmark dismissal).
        athlete, _c, session, _p = seed()
        client.force_login(athlete)
        body = client.get(session_url(session)).content.decode()
        assert ONBOARDING_JS in body
        assert INSTALL_CARD not in body

    def test_coach_roster_has_no_onboarding_chrome(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        client.force_login(coach)
        body = client.get(ROSTER).content.decode()
        assert ONBOARDING_JS not in body
        assert INSTALL_CARD not in body
