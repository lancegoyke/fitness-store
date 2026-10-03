"""#709 PR 2 (server half) — the live-sync change stamp and the two poll endpoints.

``Plan.sync_version`` is bumped by every write either screen can see, read
BEFORE the rows it describes, echoed in write answers, and compared by two
cheap GET polls. Every test here needs the new field or URL, so each fails on
unmodified main (AttributeError / NoReverseMatch). The exceptions are the
"does NOT bump" guards, which pass trivially on main; they are proved by
mutation, noted on each.
"""

import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from store_project.meso import models
from store_project.meso import settle
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def version(plan):
    return Plan.objects.get(pk=plan.pk).sync_version


def modified(plan):
    return Plan.objects.get(pk=plan.pk).modified


def sync_url(plan):
    return reverse("meso:api_plan_sync", kwargs={"plan_id": plan.pk})


def athlete_sync_url(session):
    return reverse("meso:athlete_session_sync", kwargs={"pk": session.pk})


# -- the field ----------------------------------------------------------------


class TestField:
    def test_a_new_plan_starts_at_zero_with_a_database_default(self):
        s = seed()
        assert version(s.plan) == 0
        field = Plan._meta.get_field("sync_version")
        assert field.db_default == 0
        assert field.default == 0


# -- the bump on every write path ---------------------------------------------


class TestBump:
    def test_a_coach_line_write_bumps_by_one_and_moves_modified_with_it(self, client):
        s = seed()
        before_modified = modified(s.plan)
        client.force_login(s.coach)
        assert coach_write(client, s, "Pause at the bottom").status_code == 200
        assert version(s.plan) == 1
        assert modified(s.plan) > before_modified

    def test_touch_plan_is_one_statement(self):
        from store_project.meso import views

        s = seed()
        with CaptureQueriesContext(connection) as ctx:
            views._touch_plan(s.plan)
        updates = [q for q in ctx.captured_queries if q["sql"].startswith("UPDATE")]
        assert len(ctx.captured_queries) == 1
        assert len(updates) == 1
        assert "sync_version" in updates[0]["sql"]
        assert "modified" in updates[0]["sql"]
        assert version(s.plan) == 1

    def test_a_prescription_patch_bumps(self, client):
        s = seed()
        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"text": "3x5 @ 80%"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        assert version(s.plan) == 1

    @pytest.mark.parametrize("verb", ["api_plan_undo", "api_plan_redo"])
    def test_undo_and_redo_bump(self, client, verb):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "Pause at the bottom")
        if verb == "api_plan_redo":
            client.post(
                reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk}),
                content_type="application/json",
            )
        before = version(s.plan)
        resp = client.post(
            reverse(f"meso:{verb}", kwargs={"plan_id": s.plan.pk}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        assert version(s.plan) == before + 1

    def test_an_athlete_blur_that_changes_text_bumps(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200
        assert version(s.plan) == 1

    def test_the_unskip_repair_repost_bumps(self, client):
        from store_project.meso.models import LoggedSet
        from store_project.meso.models import Prescription

        # A COACH set line with no set behind it (logged while the row was
        # skipped): the athlete's unchanged blur is `unchanged_coach_set`, which
        # saves nothing and skips `_touch_plan`, yet the writer derives the set.
        s = seed()
        line = sub_line(s.squat, "225 x 5", line=1)
        Prescription.objects.filter(pk=line.pk).update(
            athlete_authored=True, entered_by_coach=True
        )
        SessionLog.objects.create_for_pair(s.session, s.athlete)
        client.force_login(s.athlete)
        assert not LoggedSet.objects.filter(session_log__session=s.session).exists()
        before = version(s.plan)
        assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200
        assert LoggedSet.objects.filter(session_log__session=s.session).exists()
        assert version(s.plan) > before

    def test_an_unchanged_coach_cue_blur_does_not_bump(self, client):
        # Guard: passes on main only because the field is absent from the
        # assertion's reach (AttributeError) -- red there. Mutation proof: make
        # the athlete path call _touch_plan unconditionally and this fails.
        s = seed()
        sub_line(s.squat, "Pause at the bottom", line=1)
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "Pause at the bottom")
        assert resp.status_code == 200
        assert version(s.plan) == 0

    def test_agent_apply_bumps(self, client):
        plan, _, presc_ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc_,
            payload={"name": "Box Squat"},
            status=ProposedChange.Status.APPROVED,
        )
        before_modified = modified(plan)
        client.force_login(plan.coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED
        assert version(plan) == 1
        assert modified(plan) > before_modified

    def test_finish_bumps_but_leaves_modified_alone(self, client):
        s = seed()
        before_modified = modified(s.plan)
        client.force_login(s.athlete)
        assert log_post(client, s.session, {"status": "done"}).status_code == 200
        assert version(s.plan) == 1
        assert modified(s.plan) == before_modified

    def test_notes_bump(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {"notes": "felt heavy"})
        assert version(s.plan) == 1

    def test_a_repeated_identical_finish_does_not_bump_again(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {"status": "done", "date": "2026-10-01"})
        assert version(s.plan) == 1
        log_post(client, s.session, {"status": "done", "date": "2026-10-01"})
        assert version(s.plan) == 1

    def test_a_blank_note_with_no_log_does_not_bump(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(client, s.session, {"notes": "  "})
        assert resp.status_code == 200
        assert not SessionLog.objects.exists()
        assert version(s.plan) == 0
        assert resp.json()["sync_v"] == 0

    def test_settle_bumps_on_a_status_flip_only(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        after_write = version(s.plan)
        before_modified = modified(s.plan)
        cutoff = timezone.now()
        SessionLog.objects.filter(pk=log.pk).update(
            last_activity_at=cutoff - timedelta(hours=48)
        )
        assert settle.settle_log(log.pk, cutoff=cutoff - timedelta(hours=24)) is True
        assert version(s.plan) == after_write + 1
        assert modified(s.plan) == before_modified
        # Already DONE: nothing to flip, no bump.
        assert settle.settle_log(log.pk, cutoff=cutoff) is False
        assert version(s.plan) == after_write + 1


# -- the stamp is read FIRST --------------------------------------------------

STAMP_SQL = 'SELECT "meso_plan"."sync_version" AS "sync_version" FROM'


def _first_index(queries, needle):
    return next(i for i, q in enumerate(queries) if needle in q["sql"])


class TestReadFirst:
    def test_the_grid_reads_the_stamp_before_any_grid_row(self):
        from store_project.meso.serializers import serialize_mesocycle_grid

        s = seed()
        models.bump_plan_sync(s.plan.pk)
        mesocycle = models.Mesocycle.objects.get(pk=s.meso.pk)
        mesocycle.plan  # noqa: B018  (resolve the plan so it isn't a query below)
        with CaptureQueriesContext(connection) as ctx:
            grid = serialize_mesocycle_grid(mesocycle)
        assert grid["sync_v"] == 1
        stamp = next(
            i for i, q in enumerate(ctx.captured_queries) if STAMP_SQL in q["sql"]
        )
        assert stamp == 0
        assert _first_index(ctx.captured_queries, "meso_week") > stamp

    def test_the_athlete_page_reads_the_stamp_before_any_row(self, client):
        from store_project.meso import presenters

        s = seed()
        models.bump_plan_sync(s.plan.pk)
        session = models.Session.objects.select_related(
            "week__mesocycle__plan__relationship"
        ).get(pk=s.session.pk)
        with CaptureQueriesContext(connection) as ctx:
            ctx_dict = presenters.athlete_session(session, s.athlete)
        assert ctx_dict["sync_v"] == 1
        stamp = next(
            i for i, q in enumerate(ctx.captured_queries) if STAMP_SQL in q["sql"]
        )
        assert stamp == 0
        assert presenters.athlete_log_payload(ctx_dict)["sync_v"] == 1

    def test_the_designer_hydration_and_api_grid_carry_it(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        models.bump_plan_sync(plan.pk)
        client.force_login(link.coach)
        resp = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})
        )
        assert resp.json()["sync_v"] == 1


# -- write answers carry the stamp --------------------------------------------


class TestWriteAnswers:
    def test_coach_line_write_200_and_422(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        # 422: a changed write to the athlete's own line.
        resp = coach_write(client, s, "Something else", line=1)
        assert resp.status_code == 422
        assert resp.json()["sync_v"] == version(s.plan)
        ok = coach_write(client, s, "A cue", line=2)
        assert ok.status_code == 200
        assert ok.json()["sync_v"] == version(s.plan) == 2

    def test_prescription_patch(self, client):
        s = seed()
        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"text": "3x5"}),
            content_type="application/json",
        )
        assert resp.json()["sync_v"] == version(s.plan) == 1

    def test_athlete_cell_write_200_and_422(self, client):
        s = seed()
        sub_line(s.squat, "Pause at the bottom", line=2)
        client.force_login(s.athlete)
        ok = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert ok.json()["sync_v"] == version(s.plan) == 1
        refused = write_cell(client, s.session, s.squat, 2, "225 x 5")
        assert refused.status_code == 422
        assert refused.json()["sync_v"] == version(s.plan)

    def test_athlete_log_session(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = log_post(client, s.session, {"status": "done"})
        assert resp.json()["sync_v"] == version(s.plan) == 1


# -- coach poll ---------------------------------------------------------------


def auth_cost(client, url):
    """Queries a request spends BEFORE the view (session + user), measured on a 400."""
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 400
    return len(ctx.captured_queries)


class TestCoachPoll:
    def test_unchanged_is_one_query_beyond_auth(
        self, client, django_assert_num_queries
    ):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        n = auth_cost(client, sync_url(plan))  # no ?v= -> 400 right after auth
        with django_assert_num_queries(n + 1):
            resp = client.get(sync_url(plan), {"v": 0})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "changed": False, "sync_v": 0}

    def test_changed_returns_the_grid_with_its_own_stamp(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        models.bump_plan_sync(plan.pk)
        client.force_login(link.coach)
        resp = client.get(sync_url(plan), {"v": 0})
        body = resp.json()
        assert body["ok"] is True
        assert body["changed"] is True
        assert body["sync_v"] == 1
        assert body["grid"]["sync_v"] == 1
        assert body["grid"]["weeks"]

    def test_a_newer_client_stamp_also_reads_as_changed(self, client):
        # != not >: a restored database can move the stamp backwards.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        assert client.get(sync_url(plan), {"v": 99}).json()["changed"] is True

    def test_mesocycle_param_is_resolved_like_the_grid(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        other = CoachAthleteFactory().create_plan().mesocycles.get()
        client.force_login(link.coach)
        mine = plan.mesocycles.get()
        ok = client.get(sync_url(plan), {"v": 99, "mesocycle": mine.pk})
        assert ok.json()["grid"]["mesocycle"]["id"] == mine.pk
        assert (
            client.get(sync_url(plan), {"v": 99, "mesocycle": other.pk}).status_code
            == 404
        )
        assert (
            client.get(sync_url(plan), {"v": 99, "mesocycle": "x"}).status_code == 400
        )

    @pytest.mark.parametrize("v", [None, "", "x", "-1", "1.5", " 7", "+7"])
    def test_a_missing_or_bad_v_is_400(self, client, v):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        params = {} if v is None else {"v": v}
        assert client.get(sync_url(plan), params).status_code == 400

    def test_scoping(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(UserFactory())
        assert client.get(sync_url(plan), {"v": 0}).status_code == 403
        client.force_login(link.athlete)  # the athlete is not the coach
        assert client.get(sync_url(plan), {"v": 0}).status_code == 403
        client.force_login(link.coach)
        missing = reverse("meso:api_plan_sync", kwargs={"plan_id": 10**9})
        assert client.get(missing, {"v": 0}).status_code == 404

    def test_anonymous_is_redirected_to_login(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        assert client.get(sync_url(plan), {"v": 0}).status_code == 302

    def test_a_template_plan_polls_for_its_owner(self, client):
        # Same editability rule as the grid: owner of a relationship-less plan.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        Plan.objects.filter(pk=plan.pk).update(
            is_template=True, relationship=None, owner=link.coach
        )
        client.force_login(link.coach)
        assert client.get(sync_url(plan), {"v": 0}).json()["changed"] is False

    def test_the_poll_is_not_cacheable(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        cc = client.get(sync_url(plan), {"v": 0})["Cache-Control"]
        assert "no-store" in cc or "no-cache" in cc


# -- athlete poll -------------------------------------------------------------


class TestAthletePoll:
    def test_unchanged_is_one_query_beyond_auth(
        self, client, django_assert_num_queries
    ):
        s = seed()
        client.force_login(s.athlete)
        n = auth_cost(client, athlete_sync_url(s.session))
        with django_assert_num_queries(n + 1):
            resp = client.get(athlete_sync_url(s.session), {"v": 0})
        assert resp.json() == {"ok": True, "changed": False, "sync_v": 0}

    def test_a_coach_line_shows_up_in_the_changed_payload(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "Pause at the bottom")
        client.force_login(s.athlete)
        body = client.get(athlete_sync_url(s.session), {"v": 0}).json()
        assert body["ok"] is True
        assert body["changed"] is True
        assert body["sync_v"] == version(s.plan) == 1
        squat = next(e for e in body["exercises"] if e["id"] == s.squat.pk)
        assert squat["coach_lines"] == [{"line": 1, "text": "Pause at the bottom"}]
        for key in ("sub_lines", "logged_readonly", "pad_lines"):
            assert key in squat
        assert body["progress"]["as_of"]
        assert body["status"] == "pending"
        assert "notes" in body

    def test_a_finished_session_changes_status_for_the_other_screen(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(client, s.session, {"status": "done", "notes": "good"})
        body = client.get(athlete_sync_url(s.session), {"v": 0}).json()
        assert (body["status"], body["notes"]) == ("done", "good")

    @pytest.mark.parametrize("v", [None, "x", "-3"])
    def test_a_missing_or_bad_v_is_400(self, client, v):
        s = seed()
        client.force_login(s.athlete)
        params = {} if v is None else {"v": v}
        assert client.get(athlete_sync_url(s.session), params).status_code == 400

    def test_a_foreign_session_is_404(self, client):
        s = seed()
        client.force_login(UserFactory())
        assert client.get(athlete_sync_url(s.session), {"v": 0}).status_code == 404
        client.force_login(s.coach)  # the coach is not the athlete
        assert client.get(athlete_sync_url(s.session), {"v": 0}).status_code == 404

    def test_a_deleted_session_is_404(self, client):
        s = seed()
        models.Session.objects.filter(pk=s.session.pk).update(deleted_at=timezone.now())
        client.force_login(s.athlete)
        assert client.get(athlete_sync_url(s.session), {"v": 0}).status_code == 404

    def test_anonymous_is_redirected(self, client):
        s = seed()
        assert client.get(athlete_sync_url(s.session), {"v": 0}).status_code == 302

    def test_the_page_hydration_carries_the_stamp(self, client):
        s = seed()
        models.bump_plan_sync(s.plan.pk)
        client.force_login(s.athlete)
        resp = client.get(reverse("meso:athlete_session", kwargs={"pk": s.session.pk}))
        assert resp.context["log_data"]["sync_v"] == 1


# -- the service worker never answers the poll --------------------------------


class TestServiceWorker:
    def test_sync_urls_are_not_static_and_the_worker_passes_them_through(self, client):
        from django.conf import settings

        s = seed()
        for url in (sync_url(s.plan), athlete_sync_url(s.session)):
            assert url.startswith("/meso/api/")
            assert not url.startswith(settings.STATIC_URL)
        text = client.get(reverse("meso:service_worker")).content.decode()
        # The only respondWith on a non-navigation GET sits AFTER the static
        # prefix early-return, so anything under /meso/api/ falls through to
        # the network untouched.
        guard = text.index("if (!url.pathname.startsWith(STATIC_PREFIX)) return;")
        assert text.index("event.respondWith(", guard) > guard
        # Before the guard the only respondWith is the navigation branch's.
        before = text[:guard]
        assert before.count("event.respondWith(") == 1
        assert before.index("if (isNavigation(request))") < before.index(
            "event.respondWith("
        )


# -- review round 1 -----------------------------------------------------------


def _rename_and_bump(plan, title):
    """A committed coach rename: the title changes and the stamp moves with it."""
    Plan.objects.filter(pk=plan.pk).update(title=title)
    models.bump_plan_sync(plan.pk)


class TestStampCoversThePlanRowsToo:
    """The stamp comes from the row read that loaded the Plan, not a later one.

    A rename committing between the access check and the serializer used to
    give a payload with the OLD title under the NEW stamp: the poll then called
    it current and never repaired it.
    """

    def _race(self, monkeypatch, link, plan):
        from store_project.meso import views

        real = views._default_grid_mesocycle

        def renaming(p):
            # The access check already ran with the old row; now the rename lands.
            _rename_and_bump(plan, "Renamed")
            return real(p)  # `p.mesocycles` hands the OLD plan instance down

        monkeypatch.setattr(views, "_default_grid_mesocycle", renaming)

    def _assert_consistent(self, grid):
        # Never a stamp that claims the rename while the title predates it.
        assert not (grid["sync_v"] >= 1 and grid["plan"]["title"] != "Renamed")

    def test_the_grid_endpoint(self, client, monkeypatch):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        self._race(monkeypatch, link, plan)
        client.force_login(link.coach)
        resp = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})
        )
        self._assert_consistent(resp.json())
        assert resp.json()["sync_v"] == 0

    def test_the_poll(self, client, monkeypatch):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        self._race(monkeypatch, link, plan)
        client.force_login(link.coach)
        body = client.get(sync_url(plan), {"v": 99}).json()
        self._assert_consistent(body["grid"])
        assert body["sync_v"] == body["grid"]["sync_v"] == 0

    def test_the_designer_hydration(self, client, monkeypatch):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        self._race(monkeypatch, link, plan)
        client.force_login(link.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        grid = resp.context["grid_data"]
        self._assert_consistent(grid)
        assert grid["sync_v"] == 0

    def test_without_a_passed_stamp_the_rows_are_refetched_after_it(self):
        from store_project.meso.serializers import serialize_mesocycle_grid

        link = CoachAthleteFactory()
        plan = link.create_plan()
        stale = models.Mesocycle.objects.select_related("plan").get(
            plan=plan
        )  # loaded BEFORE the rename
        _rename_and_bump(plan, "Renamed")
        grid = serialize_mesocycle_grid(stale)
        assert grid["sync_v"] == 1
        assert grid["plan"]["title"] == "Renamed"

    def test_the_athlete_page(self, client, monkeypatch):
        from store_project.meso import presenters

        s = seed()
        real = presenters.athlete_session

        def renaming(session, athlete, **kw):
            _rename_and_bump(s.plan, "Renamed")
            return real(session, athlete, **kw)

        monkeypatch.setattr(presenters, "athlete_session", renaming)
        client.force_login(s.athlete)
        resp = client.get(reverse("meso:athlete_session", kwargs={"pk": s.session.pk}))
        ctx = resp.context["session"]
        assert not (ctx["sync_v"] >= 1 and ctx["plan_title"] != "Renamed")
        assert resp.context["log_data"]["sync_v"] == 0

    def test_the_athlete_poll(self, client, monkeypatch):
        from store_project.meso import presenters

        s = seed()
        real = presenters.athlete_session
        seen = {}

        def renaming(session, athlete, **kw):
            _rename_and_bump(s.plan, "Renamed")
            out = real(session, athlete, **kw)
            seen["title"] = out["plan_title"]
            return out

        monkeypatch.setattr(presenters, "athlete_session", renaming)
        client.force_login(s.athlete)
        body = client.get(athlete_sync_url(s.session), {"v": 99}).json()
        assert not (body["sync_v"] >= 1 and seen["title"] != "Renamed")
        assert body["sync_v"] == 0

    def test_athlete_session_without_a_stamp_refetches_after_it(self):
        from store_project.meso import presenters

        s = seed()
        stale = models.Session.objects.select_related(
            "week__mesocycle__plan__relationship"
        ).get(pk=s.session.pk)
        _rename_and_bump(s.plan, "Renamed")
        ctx = presenters.athlete_session(stale, s.athlete)
        assert ctx["sync_v"] == 1
        assert ctx["plan_title"] == "Renamed"


class TestPerformanceLineToken:
    def test_an_athlete_line_carries_its_client_token(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            reverse("meso:athlete_cell_write", kwargs={"pk": s.session.pk}),
            data=json.dumps(
                {
                    "exercise_id": s.squat.pk,
                    "line": 1,
                    "text": "225 x 5",
                    "new": True,
                    "token": "tok-abc",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        body = client.get(athlete_sync_url(s.session), {"v": 0}).json()
        squat = next(e for e in body["exercises"] if e["id"] == s.squat.pk)
        assert [x["token"] for x in squat["sub_lines"]] == ["tok-abc"]

    def test_a_line_without_one_carries_an_empty_string(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        body = client.get(athlete_sync_url(s.session), {"v": 0}).json()
        squat = next(e for e in body["exercises"] if e["id"] == s.squat.pk)
        assert [x["token"] for x in squat["sub_lines"]] == [""]


class TestVCap:
    @pytest.mark.parametrize("which", ["coach", "athlete"])
    def test_more_than_18_digits_is_400_and_18_is_fine(self, client, which):
        s = seed()
        if which == "coach":
            client.force_login(s.coach)
            url = sync_url(s.plan)
        else:
            client.force_login(s.athlete)
            url = athlete_sync_url(s.session)
        assert client.get(url, {"v": "9" * 19}).status_code == 400
        assert client.get(url, {"v": "9" * 18}).status_code == 200


class TestManualOneRmBumps:
    def test_the_athletes_manual_1rm_bumps_sync_only(self, client):
        s = seed()
        before_modified = modified(s.plan)
        client.force_login(s.athlete)
        resp = client.post(
            reverse("meso:athlete_set_one_rm", kwargs={"pk": s.session.pk}),
            data=json.dumps({"prescription": s.squat.pk, "value": "140"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        assert version(s.plan) == 1
        assert modified(s.plan) == before_modified

    def test_the_coachs_manual_1rm_bumps_sync_only(self, client):
        s = seed()
        before_modified = modified(s.plan)
        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_coach_set_one_rm",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"value": "140"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        assert version(s.plan) == 1
        assert modified(s.plan) == before_modified

    def test_a_refused_write_does_not_bump(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            reverse("meso:athlete_set_one_rm", kwargs={"pk": s.session.pk}),
            data=json.dumps({"prescription": s.squat.pk, "value": "abc"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert version(s.plan) == 0
