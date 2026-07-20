"""Parse-at-commit — the write hook (5a stages 3+4, docs/meso/parse-at-commit-plan.md §5, §7, §8).

``athlete_cell_write`` (the freeform sub-line save, Phase 4a) now also parses
the just-saved text with ``parse_performed`` and upserts a silent, derivative
``LoggedSet`` scoped to ``(session_log, source_line)`` — delete-then-recreate,
idempotent, never blocking entry. This mirrors the structured logger
(``athlete_log_session``) closely enough that the two write paths must never
clobber each other: the structured logger's own delete is scoped to
``source_line__isnull=True`` so a "Save progress" never wipes a freeform-parsed
set (and vice versa, since the cell-write upsert only ever touches its own
``source_line``).

Stage 4 adds two more things to the same response, both inside the same
tolerance guard: an **optimistic PR toast** (``new_records``, §7 — off the LIVE,
PENDING-inclusive read in ``personal_records.py``) and a **derive-on-read
``warn`` flag** (§8 — re-classifies the just-committed text, no stored column).
"""

import json
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.parsing import parse_performed
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(
    *,
    coach=None,
    athlete=None,
    delivered=True,
    link_status=CoachAthlete.Status.ACTIVE,
    plan_status=Plan.Status.ACTIVE,
):
    """A minimal plan → (optionally delivered) week → session → two line-0 cells."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(coach=coach, athlete=athlete, status=link_status)
    plan = PlanFactory(relationship=rel, title="Hypertrophy Block", status=plan_status)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=2,
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name="Lower", bias="Quad")
    squat = presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    rdl = presc(session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8")
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


def cell_url(session):
    return reverse("meso:athlete_cell_write", kwargs={"pk": session.pk})


def cell_post(client, session, payload):
    return client.post(
        cell_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


def write_cell(client, session, exercise, line, text):
    return cell_post(
        client, session, {"exercise_id": exercise.pk, "line": line, "text": text}
    )


def log_url(session):
    return reverse("meso:athlete_log_session", kwargs={"pk": session.pk})


def log_post(client, session, payload):
    return client.post(
        log_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


def the_log(session, athlete):
    return SessionLog.objects.get(session=session, athlete=athlete)


def sub_cell(exercise, line=1):
    return Prescription.objects.get(
        exercise_slot=exercise.exercise_slot, week=exercise.week, line=line
    )


# -- upsert / idempotency ----------------------------------------------------


class TestParsedSetUpsert:
    def test_set_text_creates_a_logged_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        log = the_log(s.session, s.athlete)
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(session_log=log)
        assert row.source_line_id == cell.pk
        assert row.prescription_id == s.squat.pk
        assert row.set_number == 1
        assert row.load == "225"
        assert row.reps == "5"

    def test_reblur_replaces_not_appends(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        log = the_log(s.session, s.athlete)
        cell = sub_cell(s.squat, 1)
        rows = list(LoggedSet.objects.filter(session_log=log, source_line=cell))
        assert len(rows) == 1
        assert rows[0].load == "230"
        assert rows[0].reps == "3"

    def test_blank_text_deletes_the_parsed_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        resp = write_cell(client, s.session, s.squat, 1, "")
        assert resp.status_code == 200
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_unparseable_text_writes_no_set_but_keeps_text_and_returns_200(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "felt tight")
        assert resp.status_code == 200

        cell = sub_cell(s.squat, 1)
        assert cell.text == "felt tight"
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    @pytest.mark.parametrize("text", ["skip", "-", "DB pullover", "20-60m", "225 x"])
    def test_non_set_classifications_write_no_set(self, client, text):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        assert cell.text == text
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_two_sub_lines_make_two_rows_with_distinct_source_line(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 2, "230 x 3")

        log = the_log(s.session, s.athlete)
        cell1 = sub_cell(s.squat, 1)
        cell2 = sub_cell(s.squat, 2)
        rows = list(LoggedSet.objects.filter(session_log=log).order_by("source_line"))
        assert len(rows) == 2
        assert {r.source_line_id for r in rows} == {cell1.pk, cell2.pk}
        assert {r.prescription_id for r in rows} == {s.squat.pk}

    def test_bare_load_writes_a_partial_set(self, client):
        # ``225`` alone: load, no reps — still a set (pinned in the parser
        # corpus, plan §3).
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225")
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(source_line=cell)
        assert row.load == "225"
        assert row.reps == ""


# -- collision between the two write paths -----------------------------------


class TestStructuredAndFreeformCollision:
    def test_freeform_then_structured_save_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        parsed_pk = LoggedSet.objects.get(source_line=cell).pk

        resp = log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "8",
                    }
                ]
            },
        )
        assert resp.status_code == 200

        log = the_log(s.session, s.athlete)
        # The parsed set survives untouched...
        parsed = LoggedSet.objects.get(pk=parsed_pk)
        assert parsed.source_line_id == cell.pk
        assert parsed.load == "225"
        # ...the structured set was created alongside it, no duplicate.
        structured = LoggedSet.objects.get(source_line__isnull=True, session_log=log)
        assert structured.prescription_id == s.rdl.pk
        assert LoggedSet.objects.filter(session_log=log).count() == 2

    def test_structured_then_freeform_write_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "70",
                        "rpe": "7",
                    }
                ]
            },
        )
        log = the_log(s.session, s.athlete)
        structured_pk = LoggedSet.objects.get(
            session_log=log, source_line__isnull=True
        ).pk

        resp = write_cell(client, s.session, s.rdl, 1, "80 x 8")
        assert resp.status_code == 200

        # The structured set is untouched by the freeform write...
        structured = LoggedSet.objects.get(pk=structured_pk)
        assert structured.prescription_id == s.squat.pk
        # ...and the new parsed set was added alongside it.
        cell = sub_cell(s.rdl, 1)
        parsed = LoggedSet.objects.get(source_line=cell)
        assert parsed.load == "80"
        assert parsed.reps == "8"
        assert LoggedSet.objects.filter(session_log=log).count() == 2

    def test_structured_save_progress_does_not_wipe_parsed_sets(self, client):
        # The critical regression this stage guards against: the structured
        # logger's delete used to be scoped to ``prescription_id__in=trainable
        # cells`` only — and a parsed set's ``prescription`` IS a trainable
        # line-0 cell, so an unrelated "Save progress" would silently wipe it.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        # A "Save progress" that doesn't even mention the squat.
        resp = log_post(client, s.session, {"sets": [], "status": "pending"})
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(source_line=cell).exists()


# -- SessionLog status --------------------------------------------------------


class TestSessionLogStatus:
    def test_new_parsed_set_creates_a_pending_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.PENDING

    def test_cell_write_never_downgrades_a_done_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        # The structured logger's default status is DONE.
        log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "8",
                    }
                ]
            },
        )
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.DONE

        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_cell_write_reuses_the_newest_existing_log(self, client):
        # Mirrors athlete_log_session's ``-created_at`` newest-first lookup —
        # a stray second log row (however it happened) must not fork a new,
        # unrelated log with a fresh PENDING status.
        s = seed()
        first = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        newest = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 2
        )
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(source_line=cell)
        assert row.session_log_id == newest.pk
        first.refresh_from_db()
        assert first.sets.count() == 0


# -- optimistic PR toast (5a stage 4, plan §7) --------------------------------
#
# A blur that parses into a set can beat the athlete's current LIVE best —
# mirrors athlete_log_session's wiring of new_records_in/serialize_new_record,
# but off the live (PENDING-inclusive, personal_records.py) read, so it can
# fire before the session ever reaches DONE.


class TestOptimisticPrToast:
    def test_first_ever_set_is_reported_as_a_first_pr(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "120 x 5")
        assert resp.status_code == 200
        prs = resp.json()["new_records"]
        assert len(prs) == 1
        assert prs[0]["name"] == "Box Squat"
        assert prs[0]["is_first"] is True
        assert prs[0]["previous"] is None
        assert prs[0]["value"] == "140"  # 120 * (1 + 5/30)

    def test_beating_a_prior_pending_write_reports_the_delta(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "120 x 5")  # live best: 140

        other = day(s.week, day_number=2, name="Upper", order=2)
        other_squat = presc(other, name="Box Squat", order=0, text="")
        resp = write_cell(client, other, other_squat, 1, "150 x 5")  # 175
        prs = resp.json()["new_records"]
        assert len(prs) == 1
        assert prs[0]["is_first"] is False
        assert prs[0]["previous"] == "140"
        assert prs[0]["value"] == "175"

    def test_a_lighter_write_reports_no_new_records(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "150 x 5")  # live best: 175

        other = day(s.week, day_number=2, name="Upper", order=2)
        other_squat = presc(other, name="Box Squat", order=0, text="")
        resp = write_cell(client, other, other_squat, 1, "120 x 5")  # 140, lighter
        assert resp.status_code == 200
        assert resp.json()["new_records"] == []

    def test_a_non_set_write_reports_no_new_records(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "felt tight")
        assert resp.status_code == 200
        assert resp.json()["new_records"] == []

    def test_correcting_a_cell_self_heals_the_live_best(self, client):
        # "Live and self-healing" (plan §7): a re-blur that lowers the load
        # changes what the next read of the records panel returns, no
        # invalidation step needed.
        from store_project.meso.presenters import athlete_personal_records

        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "120 x 5")
        assert athlete_personal_records(s.athlete)["rows"][0]["e1rm"] == "140"

        write_cell(client, s.session, s.squat, 1, "90 x 5")  # the athlete corrects it
        rows = athlete_personal_records(s.athlete)["rows"]
        assert rows[0]["e1rm"] == "105"  # 90 * (1 + 5/30)


# -- warn flag (5a stage 4, plan §8) ------------------------------------------
#
# Derive-on-read from the just-committed text — no stored column. Only the
# `unresolved-set` classification (a fat-fingered set attempt) warns; every
# other classification (a real set, skip/swap/note/duration) does not. The
# classification corpus itself is pinned in test_parsing.py; here we pin that
# the write endpoint surfaces it on the response.


class TestCellWriteWarnFlag:
    @pytest.mark.parametrize(
        "text,expected_warn",
        [
            ("225 x", True),  # unresolved-set: looks like a set, doesn't resolve
            ("2255x5", True),  # unresolved-set: implausible fat-fingered load
            ("225 x 5", False),  # a real set
            ("225", False),  # a bare partial set (load only)
            ("skip", False),
            ("-", False),
            ("DB pullover", False),  # swap
            ("felt tight", False),  # note
            ("20-60m", False),  # duration
            ("", False),  # blank clears the cell — parse_performed returns None
        ],
    )
    def test_response_warn_matches_the_classification(
        self, client, text, expected_warn
    ):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn"] is expected_warn

    def test_warn_clears_on_a_fixing_reblur(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp1 = write_cell(client, s.session, s.squat, 1, "225 x")
        assert resp1.json()["cell"]["warn"] is True

        resp2 = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp2.json()["cell"]["warn"] is False

    def test_warn_appears_on_a_breaking_reblur(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp1 = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp1.json()["cell"]["warn"] is False

        resp2 = write_cell(client, s.session, s.squat, 1, "225 x")
        assert resp2.json()["cell"]["warn"] is True


# -- tolerance guard -----------------------------------------------------------


class TestToleranceGuard:
    def test_parse_error_never_breaks_the_response_or_loses_text(
        self, client, monkeypatch
    ):
        def boom(_text):
            raise RuntimeError("boom")

        monkeypatch.setattr("store_project.meso.views.parse_performed", boom)

        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        cell = sub_cell(s.squat, 1)
        assert cell.text == "225 x 5"
        # No set could be derived (the parse blew up), but the write itself
        # is unharmed.
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_parse_error_is_logged(self, client, monkeypatch, caplog):
        def boom(_text):
            raise RuntimeError("boom")

        monkeypatch.setattr("store_project.meso.views.parse_performed", boom)

        s = seed()
        client.force_login(s.athlete)
        with caplog.at_level("ERROR", logger="store_project.meso.views"):
            write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert any(
            "boom" in r.message or "parse" in r.message.lower() for r in caplog.records
        )

    # NOTE: the *database*-error half of the tolerance guard — proving the
    # savepoint in ``_upsert_parsed_set`` keeps a failed upsert from rolling
    # back the athlete's cell text — cannot be tested here. Both tests above
    # raise a plain Python exception, which leaves the connection healthy, so
    # they pass with or without the savepoint. Only a genuinely aborted
    # transaction discriminates, and SQLite doesn't abort. That test lives in
    # ``test_parse_at_commit_postgres.py`` and runs in the Postgres CI job.


# -- Codex review follow-ups ---------------------------------------------------


class TestParsedSetsStayOutOfTheStructuredLogger:
    """A parsed set must not masquerade as a structured input row (plan §6).

    ``athlete_session`` already excludes them from ``set_rows``; the log
    endpoint's own response is the second surface that has to agree, because
    the client's ``syncFromLog`` maps every set it receives onto a
    ``(prescription, set_number)`` input.
    """

    def test_the_log_response_omits_parsed_sets(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        resp = log_post(client, s.session, {"status": "pending", "sets": []})
        assert resp.status_code == 200

        returned = resp.json()["log"]["sets"]
        cell = sub_cell(s.squat, 1)
        parsed = LoggedSet.objects.get(source_line=cell)
        assert parsed.pk not in {row["id"] for row in returned}, (
            "the parsed set leaked into the structured logger's response — "
            "syncFromLog would mark a set done that nobody posted"
        )

    def test_a_structured_save_still_echoes_its_own_sets(self, client):
        # The filter must not swallow the logger's own rows.
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "8",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()["log"]["sets"]) == 1


class TestOptimisticToastIsScopedToThisBlur:
    def test_an_unrelated_blur_does_not_refire_an_earlier_pr(self, client):
        """Blurring a note must not re-celebrate a PR won on another line.

        ``new_records_in`` reports every lift in the session beating its prior
        best, so an unscoped read re-returns the earlier record on every
        subsequent blur and the toast fires again and again.
        """
        s = seed()
        client.force_login(s.athlete)

        first = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert first.json()["new_records"], "the first PR should be celebrated"

        # A note on a different line — nothing was logged, so nothing to fire.
        second = write_cell(client, s.session, s.rdl, 1, "felt tight")
        assert second.json()["new_records"] == []

    def test_a_blank_cell_reports_no_records(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cleared = write_cell(client, s.session, s.squat, 1, "")
        assert cleared.json()["new_records"] == []


class TestDoneLogEditsRefreshThePersistedOneRm:
    def test_editing_a_parsed_cell_on_a_done_log_refreshes_the_one_rm(self, client):
        """A DONE log's parsed sets feed the persisted AthleteOneRm.

        Derivation is DONE-only, so once a log is DONE its parsed sets are part
        of the confirmed record — editing one has to recompute, or percent-load
        suggestions and the coach's designer keep quoting a stale estimate.
        """
        from store_project.meso.models import AthleteOneRm

        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        log = the_log(s.session, s.athlete)
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])

        # Seed the persisted estimate from the DONE state.
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        before = AthleteOneRm.objects.filter(athlete=s.athlete).first()
        assert before is not None, "a DONE parsed set should derive a 1RM"

        # A heavier set must raise it.
        write_cell(client, s.session, s.squat, 1, "315 x 5")
        after = AthleteOneRm.objects.get(pk=before.pk)
        assert after.value > before.value, (
            "the persisted 1RM went stale after a DONE parsed-cell edit"
        )


# -- Codex review round 2 ------------------------------------------------------


def reclaim(client, s, text="coach cue: brace harder", line=1):
    """The coach overwrites an athlete-authored sub-line."""
    return client.post(
        reverse(
            "meso:api_cell_line_write",
            kwargs={"plan_id": s.plan.pk, "slot_id": s.squat.exercise_slot.pk},
        ),
        data=json.dumps({"week_id": s.week.pk, "line": line, "text": text}),
        content_type="application/json",
    )


class TestReclaimLeavesAthleteDataAlone:
    """A coach edit must not touch the athlete's performance at all.

    This took three attempts. Deleting the derived set lost it with no way back
    — `history.py` keeps SessionLog/LoggedSet/AthleteOneRm out of the plan
    snapshot precisely because "undo must never touch ... athlete data", and a
    coach edit is undoable. Detaching it (clearing `source_line`) preserved the
    row but made it indistinguishable from a structured one, so the logger's own
    delete wiped it on the athlete's next save.

    The row needs no mutation. Suppression keys on `source_line.athlete_authored`
    — which `cell_line_write` already flips — so the set simply starts rendering
    again, while `source_line` stays set and keeps the structured delete off it.
    """

    def test_the_set_is_untouched_by_a_reclaim(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(source_line=cell)

        client.force_login(s.coach)
        assert reclaim(client, s).status_code == 200

        row.refresh_from_db()
        assert (row.load, row.reps) == ("225", "5")
        assert row.source_line_id == cell.pk, (
            "the link must survive — it is what keeps the structured logger's "
            "delete from treating this as one of its own rows"
        )

    def test_a_reposted_reclaimed_set_is_replaced_not_duplicated(self, client):
        """Once visible, the row is the logger's to replace — exactly once.

        The logger renders the reclaimed row, so a save reposts it. If the
        replace-delete skipped it (scoped to `source_line__isnull=True`), the
        repost would `bulk_create` a SECOND row for the same prescription and
        set number, double-counting the performance in coach results and
        recent-log grounding.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s)

        # The athlete's page now shows it, so their save carries it back.
        client.force_login(s.athlete)
        resp = log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(prescription=s.squat).count() == 1

    def test_a_hidden_parsed_set_is_still_untouchable_by_the_logger(self, client):
        """The other half of the rule: what it can't see, it can't replace."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        resp = log_post(client, s.session, {"status": "pending", "sets": []})
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(source_line=cell).exists()

    def test_the_reclaimed_set_becomes_visible_again(self, client):
        """Its text no longer shows it, so it must render as a structured row."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert all(r["load"] == "" for r in row["set_rows"]), (
            "while athlete-authored it renders as its sub-line text only"
        )

        client.force_login(s.coach)
        reclaim(client, s)

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert any(r["load"] == "225" for r in row["set_rows"]), (
            "after the reclaim nothing else displays this performance, so "
            "suppressing it would hide a set that still counts"
        )

    def test_the_reclaimed_set_reaches_the_logger_response(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s)

        client.force_login(s.athlete)
        returned = log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "",
                    }
                ],
            },
        ).json()
        assert [r["load"] for r in returned["log"]["sets"]] == ["225"]


class TestSkippedRowsDoNotLog:
    def test_a_blur_on_a_skipped_row_writes_no_set(self, client):
        """A skipped row isn't trainable, so it can't be logged.

        `athlete_cell_write` validates against `session.cells()`, which INCLUDES
        skipped cells, so an athlete on a stale page can still post here after
        the coach skips the row. The structured logger would reject the same
        work via `trainable_cells()`.
        """
        s = seed()
        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

        # The text is still saved — never block entry.
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        assert cell.text == "225 x 5"
        assert not LoggedSet.objects.filter(source_line=cell).exists()
        assert resp.json()["new_records"] == []

    def test_a_stale_blur_on_a_skipped_row_preserves_the_existing_set(self, client):
        """Skip + a stale blur must not become data loss.

        This assertion used to be the opposite — that the next blur cleared the
        set — which contradicted `prescription_skip`'s deliberate preservation
        of earned history. A skipped row is READ-ONLY to the blur path: it mints
        nothing new and destroys nothing old.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])

        # The athlete's open page fires one more blur.
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        row = LoggedSet.objects.get(source_line=cell)
        assert (row.load, row.reps) == ("225", "5")


class TestDoneSubjectsCompareAgainstSettledHistory:
    def test_a_later_pending_draft_does_not_erase_a_finished_sessions_pr(self, client):
        """A DONE session's PR must not depend on a draft saved afterwards.

        `new_records_in` powers both the live athlete toast and the coach's
        `session_results`. Once the live read started counting PENDING sets, a
        draft in a LATER session became eligible as the "prior best" for an
        already-finished one — so the coach's view of a completed session could
        silently stop showing its record because the athlete typed something
        heavier into a draft elsewhere.
        """
        from store_project.meso.personal_records import new_records_in

        s = seed()
        client.force_login(s.athlete)

        # A finished 225x5 on the squat.
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        done = the_log(s.session, s.athlete)
        done.status = SessionLog.Status.DONE
        done.save(update_fields=["status"])
        assert new_records_in(done), "the finished session should hold a PR"

        # A heavier PENDING draft in a different session.
        other = day(s.week, day_number=2, name="Lower B", bias="Quad")
        other_squat = presc(
            other, name="Box Squat", order=0, sets="3", reps="5", load="70", rpe="8"
        )
        write_cell(client, other, other_squat, 1, "315 x 5")
        draft = SessionLog.objects.get(session=other, athlete=s.athlete)
        assert draft.status == SessionLog.Status.PENDING

        assert new_records_in(done), (
            "a pending draft in another session retroactively erased the "
            "finished session's PR — a DONE subject must compare against "
            "settled history only"
        )

    def test_a_pending_subject_still_uses_the_live_baseline(self, client):
        # The optimistic path is unchanged: a live draft compares against live
        # history, which is what makes the blur toast possible at all.
        from store_project.meso.personal_records import new_records_in

        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        pending = the_log(s.session, s.athlete)
        assert pending.status == SessionLog.Status.PENDING
        assert new_records_in(pending)


class TestPersistedOneRmTracksItsSet:
    def test_a_reclaim_leaves_the_one_rm_standing(self, client):
        """The set survives a reclaim, so its estimate must too.

        This test previously asserted the opposite — that reclaiming cleared the
        1RM — back when the reclaim path deleted the set. It doesn't: deleting
        athlete data on an undoable coach edit was the bug (see
        `TestReclaimDetachesRatherThanDeletes`). The performance is merely
        detached, so it keeps counting, and the estimate should not move.
        """
        from store_project.meso.models import AthleteOneRm

        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "315 x 5")
        log = the_log(s.session, s.athlete)
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])
        write_cell(client, s.session, s.squat, 1, "315 x 5")
        before = AthleteOneRm.objects.get(athlete=s.athlete)

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        after = AthleteOneRm.objects.get(pk=before.pk)
        assert after.value == before.value

    def test_skipping_a_row_leaves_its_one_rm_standing(self, client):
        """Nothing is deleted on skip, so the estimate has nothing to lose.

        The inverse of this ran while a blur on a skipped row stripped the set.
        It doesn't: the performance survives, so its 1RM must too.
        """
        from store_project.meso.models import AthleteOneRm

        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "315 x 5")
        log = the_log(s.session, s.athlete)
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])
        write_cell(client, s.session, s.squat, 1, "315 x 5")
        before = AthleteOneRm.objects.get(athlete=s.athlete)

        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])
        write_cell(client, s.session, s.squat, 1, "315 x 5")

        assert AthleteOneRm.objects.get(pk=before.pk).value == before.value


# -- Codex review round 4 ------------------------------------------------------


class TestNonPlainRepsSurviveToTheLoggedSet:
    @pytest.mark.parametrize(
        ("text", "reps"),
        [("225 x 5-8", "5-8"), ("225 x 30s", "30s"), ("225 x AMRAP", "AMRAP")],
    )
    def test_ranges_durations_and_amrap_are_stored(self, client, text, reps):
        """Writing only `parsed["reps"]` blanked these, rendering `— @ 225`."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, text)

        row = LoggedSet.objects.get(source_line=sub_cell(s.squat, 1))
        assert row.load == "225"
        assert row.reps == reps


class TestSkippingARowPreservesEarnedHistory:
    """Skipping a row the athlete already performed does not un-perform it.

    An earlier round of review suggested deleting derived sets on skip, on the
    reasoning that a hidden row can no longer be cleared by a blur. That was
    followed and then reverted: it contradicts this codebase's settled position,
    stated in `athlete_log_session`'s delete, that a set logged against a
    since-skipped cell is HISTORY and wiping it silently destroys the athlete's
    record. A parsed set is no different from a structured one here.
    """

    def test_the_skip_endpoint_keeps_parsed_sets(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_skip",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"skipped": True}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(source_line=cell).exists(), (
            "skipping a row destroyed work the athlete had already logged"
        )

    def test_unskipping_needs_no_re_derive(self, client):
        # The corollary of not destroying: restoring the row restores nothing,
        # because nothing was lost. Parse-at-commit only runs from a blur, so a
        # destructive skip would have left records missing until the athlete
        # happened to edit that cell again.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        url = reverse(
            "meso:api_prescription_skip",
            kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
        )
        for value in (True, False):
            client.post(
                url,
                data=json.dumps({"skipped": value}),
                content_type="application/json",
            )

        row = LoggedSet.objects.get(source_line=cell)
        assert (row.load, row.reps) == ("225", "5")


# -- Codex review round 5 ------------------------------------------------------


class TestParseCreatedLogsAreWellFormed:
    def test_a_blur_created_log_is_dated(self, client):
        """A NULL date would sort FIRST under Postgres's `-date` (NULLs first).

        `athlete_log_session` stamps today even for a pending draft. When the
        first write for a session is a blur instead, an unstamped log would pose
        as the newest in recent-log grounding and record provenance would lose
        its workout date.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        log = the_log(s.session, s.athlete)
        assert log.date == timezone.localdate()

    def test_repeated_blurs_reuse_one_log(self, client):
        """Every blur can create the log, so they must all land on the same one.

        Two logs would split one workout, and since later reads take only the
        newest, the sets stranded on the older one disappear from DONE coach
        results and the 1RM refresh.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.rdl, 1, "185 x 8")
        write_cell(client, s.session, s.squat, 2, "235 x 3")

        logs = SessionLog.objects.filter(session=s.session, athlete=s.athlete)
        assert logs.count() == 1
        assert logs.first().sets.count() == 3

    def test_a_blur_after_a_structured_save_reuses_that_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {"status": "pending", "sets": []})
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 1
        )


# -- Codex review round 8 ------------------------------------------------------


class TestABlurOnlyCreatesALogWhenItHasSomethingToStore:
    def test_an_empty_sub_line_creates_no_session_log(self, client):
        """Tapping "add a line" and leaving it blank is not training activity.

        `_scroll_hint`, `_athlete_default_plan_id` and `serialize_recent_logs`
        all read ANY SessionLog as activity, so an empty dated PENDING log moved
        the athlete's last-trained week and polluted recent-log grounding.
        """
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "")

        assert resp.status_code == 200
        assert not SessionLog.objects.filter(
            session=s.session, athlete=s.athlete
        ).exists()

    @pytest.mark.parametrize("text", ["felt tight", "DB pullover", "skip", "225 x"])
    def test_a_non_set_blur_creates_no_session_log(self, client, text):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, text)

        assert not SessionLog.objects.filter(
            session=s.session, athlete=s.athlete
        ).exists()
        # The text itself is still saved — never block entry.
        assert sub_cell(s.squat, 1).text == text

    def test_an_existing_log_is_still_cleaned_up_by_a_blanking_blur(self, client):
        # The "don't create" rule must not stop a real edit from clearing a set.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        write_cell(client, s.session, s.squat, 1, "")
        assert not LoggedSet.objects.filter(source_line=cell).exists()


class TestAnUnchangedReblurDoesNotRecelebrate:
    def test_reblurring_the_same_text_fires_no_second_toast(self, client):
        """The upsert always recreates, so the row's pk is always fresh.

        The `created.pk` filter alone therefore matched on every re-blur, and
        merely focusing and leaving a PR-winning cell re-fired the celebration.
        """
        s = seed()
        client.force_login(s.athlete)
        first = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert first.json()["new_records"], "the first PR should be celebrated"

        again = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert again.json()["new_records"] == []

    def test_a_real_improvement_still_celebrates(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        better = write_cell(client, s.session, s.squat, 1, "315 x 5")
        assert better.json()["new_records"]


# -- Codex review round 9 ------------------------------------------------------


class TestClearingACellCleansUpAnEmptyLog:
    def test_blanking_the_only_parsed_set_removes_the_empty_pending_log(self, client):
        """A cleared mistype must not keep counting as training activity.

        Round 8 stopped an empty blur from CREATING a log; this is the other
        half — a log created by a real set, then emptied. `_scroll_hint`,
        `_athlete_default_plan_id` and `serialize_recent_logs` read any
        SessionLog as activity, so the leftover row moved the athlete's
        last-trained week forever.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert SessionLog.objects.filter(session=s.session).exists()

        write_cell(client, s.session, s.squat, 1, "")
        assert not SessionLog.objects.filter(session=s.session).exists()

    def test_a_log_with_other_sets_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.rdl, 1, "185 x 8")

        write_cell(client, s.session, s.squat, 1, "")
        log = the_log(s.session, s.athlete)
        assert log.sets.count() == 1

    def test_a_log_with_notes_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log = the_log(s.session, s.athlete)
        log.notes = "tweaked my back"
        log.save(update_fields=["notes"])

        write_cell(client, s.session, s.squat, 1, "")
        assert SessionLog.objects.filter(pk=log.pk).exists()

    def test_a_done_log_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log = the_log(s.session, s.athlete)
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])

        write_cell(client, s.session, s.squat, 1, "")
        assert SessionLog.objects.filter(pk=log.pk).exists()


class TestOverlongParsedValuesDoNotCorruptState:
    def test_an_overlong_load_stores_nothing_and_keeps_the_text(self, client):
        """A value past the column length raises on Postgres INSIDE the savepoint.

        The guard would swallow it and roll the DELETE back with it, leaving the
        OLD set counting while the response reported warn=false. Bounding the
        fields first means we simply store nothing.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        # A long DECIMAL, deliberately: a long bare integer is already rejected
        # by `_load_is_plausible` (>999), so it never reaches the create and
        # would exercise nothing. Decimals are exempt from that guard, so this
        # is a genuine `set` whose load is 37 chars — past the 32-char column.
        long_load = "1." + "0" * 35
        text = f"{long_load} x 5"
        assert parse_performed(text)["kind"] == "set", "must reach the create"

        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200

        cell = sub_cell(s.squat, 1)
        assert cell.text == text
        # The stale set is gone rather than silently preserved.
        assert not LoggedSet.objects.filter(source_line=cell).exists()


# -- Codex review round 10 -----------------------------------------------------


class TestEmptyLogsAreReapedOnEveryPath:
    def test_a_reclaim_leaves_the_log_and_its_set_intact(self, client):
        """A reclaim mutates no athlete data, so there is nothing to reap.

        This assertion has flipped twice as the reclaim behaviour changed —
        first it reaped the emptied log (when reclaim DELETED the set), then it
        checked the detached row. Reclaim now leaves the set exactly as it is,
        `source_line` and all.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        log = the_log(s.session, s.athlete)
        assert log.sets.count() == 1
        assert log.sets.first().source_line_id == cell.pk

    def test_a_first_blur_with_overlong_values_leaves_no_log(self, client):
        """The log is created before the length guard declines to insert.

        `previous` is None on a first blur, so the earlier correction-path-only
        cleanup didn't run and malformed input still registered as activity.
        """
        s = seed()
        client.force_login(s.athlete)

        text = "1." + "0" * 35 + " x 5"
        assert parse_performed(text)["kind"] == "set"

        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200
        assert sub_cell(s.squat, 1).text == text
        assert not SessionLog.objects.filter(session=s.session).exists()


class TestVisibilityAndDeleteScopeAgree:
    def test_a_hidden_sibling_survives_a_save_that_clears_the_reclaimed_one(
        self, client
    ):
        """The invariant, on one exercise with both kinds of row.

        Sub-line 2 is still athlete-authored, so it stays hidden and the logger
        cannot touch it. Sub-line 1 was reclaimed, so it is visible and the
        logger owns it — an empty save clears it exactly as it would any
        structured row. Scoping the delete and the visibility rule separately is
        what let these two drift: first the delete wiped the hidden one, then it
        spared the visible one and the client duplicated it.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 2, "235 x 3")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder", line=1)

        client.force_login(s.athlete)
        log_post(client, s.session, {"status": "pending", "sets": []})

        remaining = list(
            LoggedSet.objects.filter(prescription=s.squat).values_list(
                "load", flat=True
            )
        )
        assert remaining == ["235"], remaining


class TestParsedSetsGetDistinctSetNumbers:
    def test_each_sub_line_takes_its_own_set_number(self, client):
        """All-set-1 collapsed under `(prescription, set_number)`.

        Invisible while suppressed, but once a reclaim surfaced them two
        tracking lines rendered and reposted as ONE set, and results labelled
        both "set 1".
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 2, "235 x 3")

        rows = LoggedSet.objects.filter(prescription=s.squat).order_by("set_number")
        assert [(r.set_number, r.load) for r in rows] == [(1, "225"), (2, "235")]

    def test_a_reblur_keeps_the_same_number(self, client):
        # Idempotent: the number comes from the line, not a running count.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 2, "235 x 3")
        write_cell(client, s.session, s.squat, 2, "240 x 2")

        row = LoggedSet.objects.get(prescription=s.squat)
        assert (row.set_number, row.load) == (2, "240")

    def test_both_survive_and_stay_distinct_after_a_reclaim(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 2, "235 x 3")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder", line=1)
        reclaim(client, s, text="and again", line=2)

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        loads = sorted(r["load"] for r in row["set_rows"] if r["load"])
        assert loads == ["225", "235"], loads


class TestTheBlurPathOwnsOnlyAthleteLines:
    def test_a_coach_cue_that_reads_like_a_set_is_not_logged(self, client):
        """The template posts on EVERY blur, including untouched coach text.

        A coach cue reading `225 x 5` would otherwise be stamped
        athlete-authored and fabricate a pending LoggedSet — and a PR — for a
        performance the athlete never did.
        """
        s = seed()
        # A coach-authored sub-line, not the athlete's.
        coach_line = Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=s.week,
            line=1,
            text="225 x 5",
            athlete_authored=False,
        )

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert resp.status_code == 200
        assert resp.json()["new_records"] == []
        assert not LoggedSet.objects.filter(source_line=coach_line).exists()
        assert not SessionLog.objects.filter(session=s.session).exists()

    def test_blurring_a_reclaimed_line_does_not_destroy_its_set(self, client):
        """A reclaimed set is the logger's history — not the blur path's to drop.

        Merely tapping the cue the coach left behind would otherwise delete the
        athlete's real performance and refresh away a DONE 1RM with it.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        # The athlete taps the reclaimed line.
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "brace harder")
        assert resp.status_code == 200

        row = LoggedSet.objects.get(source_line=cell)
        assert (row.load, row.reps) == ("225", "5")

    def test_a_brand_new_line_is_the_athletes_own(self, client):
        # The guard must not block the normal case: a line the athlete creates.
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert resp.status_code == 200
        assert LoggedSet.objects.filter(source_line=sub_cell(s.squat, 1)).exists()


class TestAnUntouchedCoachLineIsNotClaimed:
    def test_the_ownership_flag_is_left_alone(self, client):
        """Blocking the upsert isn't enough — the flag itself must not flip.

        The earlier guard prevented the set but the caller had already stamped
        `athlete_authored=True`, which (a) re-hid a reclaimed line's set via
        `HIDDEN_PARSED_SET` while the line showed coach text, and (b) made the
        guard one-shot: the NEXT blur saw an athlete-owned line and parsed the
        coach's text anyway.
        """
        s = seed()
        coach_line = Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=s.week,
            line=1,
            text="225 x 5",
            athlete_authored=False,
        )

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        coach_line.refresh_from_db()
        assert coach_line.athlete_authored is False

    def test_repeated_blurs_never_fabricate_a_set(self, client):
        # Durability: the one-shot version created the set on the second pass.
        s = seed()
        Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=s.week,
            line=1,
            text="225 x 5",
            athlete_authored=False,
        )

        client.force_login(s.athlete)
        for _ in range(3):
            write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert not LoggedSet.objects.exists()
        assert not SessionLog.objects.filter(session=s.session).exists()

    def test_a_reclaimed_lines_set_stays_visible_across_blurs(self, client):
        """Re-hiding it would make a counting set invisible to everyone."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        for _ in range(2):
            write_cell(client, s.session, s.squat, 1, "brace harder")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert any(r["load"] == "225" for r in row["set_rows"])

    def test_a_real_edit_still_claims_the_line(self, client):
        # The guard must not freeze a coach line the athlete genuinely writes on.
        s = seed()
        Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=s.week,
            line=1,
            text="brace harder",
            athlete_authored=False,
        )

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        cell = sub_cell(s.squat, 1)
        assert cell.athlete_authored is True
        assert LoggedSet.objects.get(source_line=cell).load == "225"


class TestVisibilityFollowsTheDisplayedText:
    """Suppression asks whether the sub-line still SHOWS this performance.

    Ownership was only ever a proxy for that, and it broke in both directions:
    keying on `source_line` hid a set whose text the coach had replaced, and
    keying on `athlete_authored` double-displayed whenever a reclaim kept the
    same text or an undo restored it.
    """

    def test_a_reclaim_that_keeps_the_text_does_not_double_display(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        # The coach reclaims the line but saves the SAME text.
        client.force_login(s.coach)
        reclaim(client, s, text="225 x 5")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert row["sub_lines"] == [{"line": 1, "text": "225 x 5", "warn": False}]
        assert all(r["load"] == "" for r in row["set_rows"]), (
            "the sub-line still displays this set, so showing it again in "
            "set_rows double-displays one performance"
        )

    def test_a_reclaim_that_replaces_the_text_shows_the_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert any(r["load"] == "225" for r in row["set_rows"]), (
            "nothing displays this performance any more, so hiding it would "
            "leave a counting set invisible to everyone"
        )

    def test_a_still_displayed_set_is_not_reposted_or_duplicated(self, client):
        """The delete must agree with visibility, or the two drift again."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)

        client.force_login(s.coach)
        reclaim(client, s, text="225 x 5")

        client.force_login(s.athlete)
        returned = log_post(client, s.session, {"status": "pending", "sets": []}).json()
        assert returned["log"]["sets"] == []
        assert LoggedSet.objects.filter(source_line=cell).count() == 1


class TestEditingAReclaimedLineKeepsItsHistory:
    """A reclaim hands the set to the logger; the blur path stops owning it.

    Rounds 17-18 stopped an UNCHANGED blur from destroying it. A genuine edit
    still did: the delete was keyed on `source_line`, so typing a note over the
    coach's cue erased a performance the athlete had already earned.
    """

    @pytest.mark.parametrize("new_text", ["felt tight", "", "230 x 3"])
    def test_the_old_performance_survives_any_edit(self, client, new_text):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, new_text)
        assert resp.status_code == 200

        loads = sorted(
            LoggedSet.objects.filter(prescription=s.squat).values_list(
                "load", flat=True
            )
        )
        assert "225" in loads, (
            f"editing the reclaimed line to {new_text!r} erased the athlete's "
            "earlier performance"
        )

    def test_a_new_set_on_a_reclaimed_line_is_added_not_swapped(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        loads = sorted(
            LoggedSet.objects.filter(prescription=s.squat).values_list(
                "load", flat=True
            )
        )
        assert loads == ["225", "230"], loads

    def test_a_normal_reblur_still_replaces(self, client):
        """The boundary: on a line the athlete owns, edits replace as before."""
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        rows = LoggedSet.objects.filter(prescription=s.squat)
        assert rows.count() == 1
        assert rows.first().load == "230"


class TestSetNumbersStayDistinctAcrossReclaims:
    def test_history_and_a_new_set_on_one_line_get_different_numbers(self, client):
        """A line's preserved history already occupies its number.

        Both rows taking `cell.line` collapsed under (prescription, set_number)
        the moment a second reclaim made them both visible — `athlete_session`
        would drop one from its dict, and the next structured save could delete
        both while reposting only one.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        rows = LoggedSet.objects.filter(prescription=s.squat).order_by("set_number")
        assert [(r.set_number, r.load) for r in rows] == [(1, "225"), (2, "230")]

    def test_both_render_after_a_second_reclaim(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "230 x 3")
        client.force_login(s.coach)
        reclaim(client, s, text="and again")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        loads = sorted(r["load"] for r in row["set_rows"] if r["load"])
        assert loads == ["225", "230"], loads

    def test_an_ordinary_reblur_keeps_its_line_number(self, client):
        # The boundary: nothing else holds the number, so it stays stable.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 2, "235 x 3")
        write_cell(client, s.session, s.squat, 2, "240 x 2")

        row = LoggedSet.objects.get(prescription=s.squat)
        assert (row.set_number, row.load) == (2, "240")


class TestStructuredSavesDoNotCollideWithHiddenRows:
    def test_a_posted_set_number_pushes_the_hidden_row_aside(self, client):
        """The mirror of round 21: the collision can come from either channel.

        A hidden parsed row holding set 1 is invisible today, so a posted set 1
        looks free — until a reclaim surfaces both and they collapse in
        `athlete_session`'s (prescription, set_number) dict.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "185",
                        "rpe": "",
                    }
                ],
            },
        )

        rows = LoggedSet.objects.filter(prescription=s.squat).order_by("set_number")
        assert [(r.set_number, r.load) for r in rows] == [(1, "185"), (2, "225")]

    def test_both_render_once_the_line_is_reclaimed(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log_post(
            client,
            s.session,
            {
                "status": "pending",
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "185",
                        "rpe": "",
                    }
                ],
            },
        )

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        loads = sorted(r["load"] for r in row["set_rows"] if r["load"])
        assert loads == ["185", "225"], loads


class TestAnUnstorableSetTellsTheAthlete:
    def test_a_set_too_long_to_store_warns(self, client):
        """Silently not logging valid-looking text is the worst outcome.

        The upsert declines a value past the column limit; without this the
        response said warn=false, so the athlete saw ordinary text that never
        counted toward their records.
        """
        s = seed()
        client.force_login(s.athlete)
        text = "1." + "0" * 35 + " x 5"
        assert parse_performed(text)["kind"] == "set"

        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200
        assert resp.json()["cell"]["warn"] is True
        assert not LoggedSet.objects.exists()

    def test_the_presenter_agrees_on_reload(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "1." + "0" * 35 + " x 5")

        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert row["sub_lines"][0]["warn"] is True

    def test_an_ordinary_set_still_does_not_warn(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.json()["cell"]["warn"] is False


class TestRestoringAReclaimedLineDoesNotDuplicate:
    def test_typing_the_original_text_back_reuses_the_row(self, client):
        """Restoring is not a second performance.

        The old row survived the reclaim, so `previous_text` (the coach's cue)
        no longer describes it and `mine` is empty. Creating would leave two
        identical rows on one source line — BOTH hidden by the restored text and
        both counted, overstating the workout with nothing on screen to show it.
        """
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        rows = LoggedSet.objects.filter(prescription=s.squat)
        assert rows.count() == 1, list(rows.values_list("load", "reps"))
        assert (rows.first().load, rows.first().reps) == ("225", "5")

    def test_it_does_not_re_celebrate(self, client):
        # The performance already existed; there is no new record to fire.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.json()["new_records"] == []

    def test_a_different_value_still_adds_a_second_row(self, client):
        # The boundary: only an identical restore is a reuse.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        client.force_login(s.coach)
        reclaim(client, s, text="brace harder")

        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        loads = sorted(
            LoggedSet.objects.filter(prescription=s.squat).values_list(
                "load", flat=True
            )
        )
        assert loads == ["225", "230"]
