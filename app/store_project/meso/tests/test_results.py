"""Athlete slice Phase 3 — results feed back (the coach's results screen).

The coach's results screen retires ``mockdata.RESULTS_*``: it reads the athlete's
real ``SessionLog`` / ``LoggedSet`` rows against the prescribed targets —
completion, RPE vs target, and the flags that drive "adjust next week". The same
truth the agent already grounds on (``serialize_recent_logs``), now surfaced to
the coach.

The screen binds to a real, owned session (``results/<session_id>/``); the bare
``results/`` redirects to the coach's most-recently-logged session (fixtures
retired, mirroring the designer/deliver bare redirects). Scoping is coach-side:
only a session on a plan the coach owns through an *active* relationship renders;
anything else is a flat 404.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.presenters import session_results
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(*, coach=None, athlete=None):
    """A coach's owned, delivered session with two prescriptions (no log yet)."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory(name="Maya Okonkwo")
    rel = CoachAthleteFactory(coach=coach, athlete=athlete)
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso, index=2, is_current=True, delivered_at=timezone.now()
    )
    session = SessionFactory(week=week, day_number=1, name="Lower")
    squat = ExercisePrescriptionFactory(
        session=session,
        name="Box Squat",
        order=0,
        sets="3",
        reps="6",
        load="70",
        rpe="7",
    )
    rdl = ExercisePrescriptionFactory(
        session=session,
        name="RDL",
        order=1,
        sets="3",
        reps="8",
        load="80",
        rpe="8",
    )
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        squat=squat,
        rdl=rdl,
    )


def log_session(
    s, *, squat_sets=(), rdl_sets=(), when=None, status=SessionLog.Status.DONE
):
    """Log ``(reps, load, rpe)`` tuples against the seeded prescriptions."""
    log = SessionLogFactory(
        session=s.session,
        athlete=s.athlete,
        status=status,
        date=when or date(2026, 6, 24),
    )
    for presc, rows in ((s.squat, squat_sets), (s.rdl, rdl_sets)):
        for n, (reps, load, rpe) in enumerate(rows, start=1):
            LoggedSetFactory(
                session_log=log,
                prescription=presc,
                set_number=n,
                reps=reps,
                load=load,
                rpe=rpe,
            )
    return log


def results_url(session):
    return reverse("meso:results_session", kwargs={"session_id": session.pk})


# -- access control + scoping ----------------------------------------------


class TestResultsAccess:
    def test_requires_login(self, client):
        s = seed()
        log_session(s, squat_sets=[("6", "70", "7")])
        resp = client.get(results_url(s.session))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_owning_coach_sees_results(self, client):
        s = seed()
        log_session(s, squat_sets=[("6", "70", "7")] * 3)
        client.force_login(s.coach)
        resp = client.get(results_url(s.session))
        assert resp.status_code == 200
        assert "Maya Okonkwo" in resp.content.decode()

    def test_404_for_foreign_coach(self, client):
        s = seed()
        log_session(s, squat_sets=[("6", "70", "7")])
        client.force_login(UserFactory())  # not this athlete's coach
        assert client.get(results_url(s.session)).status_code == 404

    def test_404_unknown_session(self, client):
        client.force_login(UserFactory())
        url = reverse("meso:results_session", kwargs={"session_id": 999999})
        assert client.get(url).status_code == 404

    def test_404_when_relationship_ended(self, client):
        s = seed()
        log_session(s, squat_sets=[("6", "70", "7")])
        s.rel.status = CoachAthlete.Status.ENDED
        s.rel.save(update_fields=["status"])
        client.force_login(s.coach)
        assert client.get(results_url(s.session)).status_code == 404

    def test_unlogged_session_renders_awaiting(self, client):
        """An owned-but-unlogged session renders the awaiting state, not a 500."""
        s = seed()  # no log
        client.force_login(s.coach)
        resp = client.get(results_url(s.session))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Not logged yet" in body
        assert "3×6 @ 70 kg · RPE 7" in body  # the prescribed target still shows


class TestResultsBareRedirect:
    def test_redirects_to_latest_logged_session(self, client):
        s = seed()
        # An older logged session on another day; the newest log wins.
        older = SessionFactory(week=s.week, day_number=2, name="Upper", order=2)
        SessionLogFactory(
            session=older,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=date(2026, 6, 20),
        )
        log_session(s, squat_sets=[("6", "70", "7")], when=date(2026, 6, 24))
        client.force_login(s.coach)
        resp = client.get(reverse("meso:results"))
        assert resp.status_code == 302
        assert resp.url == results_url(s.session)

    def test_redirects_to_roster_without_logs(self, client):
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:results"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    def test_bare_ignores_another_coachs_logs(self, client):
        """A coach with no logs of their own still lands on the roster."""
        other = seed()
        log_session(other, squat_sets=[("6", "70", "7")])
        client.force_login(UserFactory())  # a coach with no athletes/logs
        resp = client.get(reverse("meso:results"))
        assert resp.url == reverse("meso:roster")


# -- the real metrics (presenter level, precise) ---------------------------


class TestSessionResultsPresenter:
    def test_rows_and_summary(self):
        s = seed()
        log_session(
            s,
            squat_sets=[("6", "70", "8.5"), ("6", "70", "8.5"), ("6", "70", "9")],
            rdl_sets=[("8", "80", "8")] * 3,
        )
        ctx = session_results(s.session)
        assert ctx["athlete"]["name"] == "Maya Okonkwo"
        assert ctx["plan_id"] == s.plan.pk
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["target"] == "3×6 @ 70 kg · RPE 7"
        assert rows["Box Squat"]["logged"] == "3×6 @ 70 kg"
        assert rows["Box Squat"]["rpe"] == "9"  # the hardest set
        assert rows["Box Squat"]["rpe_state"] == "over"
        assert rows["RDL"]["rpe_state"] == "on"
        summary = ctx["summary"]
        assert summary["logged_state"] is True
        assert summary["completion"] == 100  # 6 logged / 6 prescribed
        assert summary["flag_count"] == 1
        assert "Box Squat" in summary["flag"]

    def test_avg_rpe_delta(self):
        s = seed()
        # squat +2 RPE on 3 sets, rdl on-target on 3 sets → mean +1.0
        log_session(
            s, squat_sets=[("6", "70", "9")] * 3, rdl_sets=[("8", "80", "8")] * 3
        )
        assert session_results(s.session)["summary"]["avg_rpe_delta"] == "+1.0"

    def test_logged_label_groups_varied_reps(self):
        s = seed()
        log_session(
            s, squat_sets=[("6", "70", "7"), ("6", "70", "7"), ("4", "70", "9")]
        )
        rows = {r["name"]: r for r in session_results(s.session)["rows"]}
        assert rows["Box Squat"]["logged"] == "2×6, 1×4 @ 70 kg"

    def test_partial_completion_and_shortfall_note(self):
        s = seed()
        log_session(
            s, squat_sets=[("6", "70", "7")] * 3, rdl_sets=[("8", "80", "8")]
        )  # only 1 of 3 RDL sets
        ctx = session_results(s.session)
        assert ctx["summary"]["completion"] == 67  # 4 of 6
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["RDL"]["note"] == "1/3 sets logged"

    def test_rep_shortfall_note(self):
        """All sets done but reps fell short on one → a rep-shortfall note."""
        s = seed()  # Box Squat prescribed 3×6
        log_session(
            s, squat_sets=[("6", "70", "7"), ("6", "70", "7"), ("4", "70", "7")]
        )
        rows = {r["name"]: r for r in session_results(s.session)["rows"]}
        assert rows["Box Squat"]["note"] == "missed 2 reps on set 3"

    def test_no_flag_when_on_target(self):
        s = seed()
        log_session(
            s, squat_sets=[("6", "70", "7")] * 3, rdl_sets=[("8", "80", "8")] * 3
        )
        summary = session_results(s.session)["summary"]
        assert summary["flag_count"] == 0
        assert summary["flag"] == ""

    def test_awaiting_state_when_unlogged(self):
        s = seed()  # no log at all
        ctx = session_results(s.session)
        assert ctx["summary"]["logged_state"] is False
        assert ctx["summary"]["completion"] == 0
        assert ctx["summary"]["logged"] is None
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["logged"] == "—"
        # Targets still render so the coach sees what was prescribed.
        assert rows["Box Squat"]["target"] == "3×6 @ 70 kg · RPE 7"

    def test_load_range_when_loads_vary(self):
        s = seed()
        log_session(s, squat_sets=[("6", "60", "7"), ("6", "70", "8")])
        rows = {r["name"]: r for r in session_results(s.session)["rows"]}
        assert rows["Box Squat"]["logged"] == "2×6 @ 60–70 kg"

    def test_pending_draft_is_not_feedback(self):
        """A 'Save progress' draft (pending) renders awaiting, not completed."""
        s = seed()
        log_session(
            s, squat_sets=[("6", "70", "7")] * 3, status=SessionLog.Status.PENDING
        )
        ctx = session_results(s.session)
        assert ctx["summary"]["logged_state"] is False
        assert ctx["summary"]["completion"] == 0
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["logged"] == "—"

    def test_completion_with_free_form_set_count(self):
        """An "AMRAP"-style set cell can't divide by zero — it falls back to logged."""
        s = seed()
        s.rdl.delete()
        s.squat.sets = "AMRAP"
        s.squat.save(update_fields=["sets"])
        log_session(s, squat_sets=[("8", "70", "8"), ("6", "70", "9")])
        # Without the fallback this session would read 0% (0 prescribed sets).
        assert session_results(s.session)["summary"]["completion"] == 100


class TestResultsDraftRedirect:
    def test_bare_redirect_ignores_a_pending_draft(self, client):
        """Only a pending draft exists → the coach lands on the roster, not it."""
        s = seed()
        log_session(s, squat_sets=[("6", "70", "7")], status=SessionLog.Status.PENDING)
        client.force_login(s.coach)
        resp = client.get(reverse("meso:results"))
        assert resp.url == reverse("meso:roster")
