"""Phase 4c — the PR (personal-records) *event* surface.

The derive-on-read engine (``personal_records``) shipped in 4b with no UI. This
slice surfaces its ``new_records_in`` detector at the two moments a new best
matters:

- **athlete** — ``athlete_log_session`` returns ``new_records`` for a just-logged
  session, so the logger can celebrate a PR the instant it's logged;
- **coach** — ``session_results`` surfaces the same PRs for the session, both as a
  ``summary["new_records"]`` list and a per-row ``pr`` flag.

Both read the structured ``LoggedSet`` performed record (D4) and reuse the pinned
Epley e1RM. **5a (plan §7) relaxed the athlete host to LIVE, not DONE-only**:
``new_records_in`` itself dropped its DONE gate, so ``athlete_log_session`` now
reports a PR off a PENDING "Save progress" too (the same live read that also
powers ``athlete_cell_write``'s optimistic toast — pinned in
``test_parse_at_commit.py``). The **coach** host stays DONE-only in effect —
not because ``new_records_in`` gates on it anymore, but because
``session_results`` only ever fetches the athlete's DONE ``SessionLog`` in the
first place (a pending draft "is not feedback yet", see its docstring) — so
``new_records_in`` never even runs against a pending session there. The
engine's own edge cases (tie, exclude-self, identity, unit scoping) are pinned
in ``test_personal_records.py``; here we pin the *surface* — that each host
wires the detector in and formats it.

The e1RM math is exact for the loads chosen: Epley is ``load * (1 + reps/30)``,
so reps=5 gives ``load * 7/6`` — 120→140, 150→175 — whole numbers that pin the
formatted labels without float noise.
"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.presenters import session_results
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as make_presc

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
    week = WeekFactory(mesocycle=meso, index=2, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower")
    squat = make_presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    rdl = make_presc(
        session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8"
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


def log_url(session):
    return reverse("meso:athlete_log_session", kwargs={"pk": session.pk})


def post_log(client, session, payload):
    return client.post(
        log_url(session), data=json.dumps(payload), content_type="application/json"
    )


def results_url(session):
    return reverse("meso:results_session", kwargs={"session_id": session.pk})


def log_done(
    session, athlete, prescription, sets, *, status=SessionLog.Status.DONE, when=None
):
    """A ``SessionLog`` (DONE by default) with ``(reps, load, rpe)`` tuples."""
    sl = SessionLogFactory(
        session=session, athlete=athlete, status=status, date=when or date(2026, 6, 24)
    )
    for n, (reps, load, rpe) in enumerate(sets, start=1):
        LoggedSetFactory(
            session_log=sl,
            prescription=prescription,
            set_number=n,
            reps=reps,
            load=load,
            rpe=rpe,
        )
    return sl


def squat_set(pk, reps, load, rpe="8"):
    return {
        "prescription": pk,
        "set_number": 1,
        "reps": reps,
        "load": load,
        "rpe": rpe,
    }


# -- athlete: the log endpoint returns new PRs -----------------------------


class TestAthleteLogNewRecords:
    def test_done_first_log_is_a_pr(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post_log(
            client,
            s.session,
            {"status": "done", "sets": [squat_set(s.squat.pk, "5", "120")]},
        )
        assert resp.status_code == 200
        prs = resp.json()["new_records"]
        assert len(prs) == 1
        pr = prs[0]
        assert pr["name"] == "Box Squat"
        assert pr["is_first"] is True
        assert pr["previous"] is None
        assert pr["value"] == "140"  # 120 * (1 + 5/30) = 140
        assert pr["unit"] == "kg"

    def test_pending_save_still_reports_a_live_pr(self, client):
        # 5a (plan §7): relaxed from DONE-only — ``new_records_in`` now counts
        # PENDING sets too, so a "Save progress" (not just "Log session") can
        # surface the same optimistic toast. (Was `test_pending_save_is_not_a_pr`,
        # pinning the DONE-only gate this slice deliberately dropped for live
        # reads — see ``personal_records.py``'s module docstring.)
        s = seed()
        client.force_login(s.athlete)
        resp = post_log(
            client,
            s.session,
            {"status": "pending", "sets": [squat_set(s.squat.pk, "5", "120")]},
        )
        assert resp.status_code == 200
        prs = resp.json()["new_records"]
        assert len(prs) == 1
        assert prs[0]["is_first"] is True
        assert prs[0]["value"] == "140"  # 120 * (1 + 5/30) = 140

    def test_pr_reports_delta_over_previous_best(self, client):
        s = seed()
        # A lighter Box Squat already logged (done) on another day → prior best 140.
        other = day(s.week, day_number=2, name="Upper", order=2)
        other_squat = make_presc(other, name="Box Squat", order=0, text="")
        log_done(
            other, s.athlete, other_squat, [("5", "120", "8")], when=date(2026, 6, 20)
        )
        client.force_login(s.athlete)
        resp = post_log(
            client,
            s.session,
            {"status": "done", "sets": [squat_set(s.squat.pk, "5", "150")]},
        )
        prs = resp.json()["new_records"]
        assert len(prs) == 1
        assert prs[0]["is_first"] is False
        assert prs[0]["previous"] == "140"
        assert prs[0]["value"] == "175"  # 150 * 7/6
        assert prs[0]["delta"] == "35"

    def test_not_a_pr_when_prior_session_was_heavier(self, client):
        s = seed()
        other = day(s.week, day_number=2, name="Upper", order=2)
        other_squat = make_presc(other, name="Box Squat", order=0, text="")
        log_done(
            other, s.athlete, other_squat, [("5", "150", "8")], when=date(2026, 6, 20)
        )
        client.force_login(s.athlete)
        resp = post_log(
            client,
            s.session,
            {"status": "done", "sets": [squat_set(s.squat.pk, "5", "120")]},
        )
        assert resp.json()["new_records"] == []


# -- coach: the results screen surfaces the same PRs -----------------------


class TestSessionResultsNewRecords:
    def test_surfaces_pr_in_summary_and_row(self):
        s = seed()
        log_done(s.session, s.athlete, s.squat, [("5", "120", "8")])
        ctx = session_results(s.session)
        prs = ctx["summary"]["new_records"]
        assert len(prs) == 1
        assert prs[0]["name"] == "Box Squat"
        assert prs[0]["value"] == "140"
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["pr"] is True
        assert rows["RDL"]["pr"] is False

    def test_no_pr_when_prior_heavier(self):
        s = seed()
        other = day(s.week, day_number=2, name="Upper", order=2)
        other_squat = make_presc(other, name="Box Squat", order=0, text="")
        log_done(
            other, s.athlete, other_squat, [("5", "150", "8")], when=date(2026, 6, 20)
        )
        log_done(
            s.session, s.athlete, s.squat, [("5", "120", "8")], when=date(2026, 6, 24)
        )
        ctx = session_results(s.session)
        assert ctx["summary"]["new_records"] == []
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["pr"] is False

    def test_unlogged_session_has_no_prs(self):
        s = seed()  # no log
        ctx = session_results(s.session)
        assert ctx["summary"]["new_records"] == []
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Box Squat"]["pr"] is False

    def test_pending_draft_has_no_prs(self):
        # Still green post-5a: `session_results` only ever fetches the
        # athlete's DONE log to begin with (see its docstring), so
        # `new_records_in` never runs against a pending session here — this
        # isn't `new_records_in`'s own (now-relaxed) DONE gate at work.
        s = seed()
        log_done(
            s.session,
            s.athlete,
            s.squat,
            [("5", "120", "8")],
            status=SessionLog.Status.PENDING,
        )
        ctx = session_results(s.session)
        assert ctx["summary"]["new_records"] == []


# -- coach: the rendered results page shows the PR callout -----------------


class TestResultsRendersNewRecords:
    def test_owning_coach_sees_pr_callout(self, client):
        s = seed()
        log_done(s.session, s.athlete, s.squat, [("5", "120", "8")])
        client.force_login(s.coach)
        resp = client.get(results_url(s.session))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "New PR" in body

    def test_no_callout_without_a_pr(self, client):
        s = seed()  # unlogged
        client.force_login(s.coach)
        body = client.get(results_url(s.session)).content.decode()
        assert "New PR" not in body
