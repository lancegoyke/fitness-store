"""Per-event tests for first-party usage analytics call sites (#509).

Drives the real views (and ``settle.settle_log`` directly) and asserts the
exact ``Event`` rows each call site produces: count, ``name``, ``actor``,
``subject_type``/``subject_id``, and ``props``. Reuses the setup helpers each
call site's own tests already established rather than inventing new fixtures
— see the imports below for where each one comes from.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import settle
from store_project.meso.agent import client as client_module
from store_project.meso.billing import webhooks as billing_webhooks
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import PushSubscription
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_agent_apply_endpoint import apply_url
from store_project.meso.tests.test_agent_apply_endpoint import make_batch_with_swap
from store_project.meso.tests.test_agent_endpoint import agent_url
from store_project.meso.tests.test_agent_endpoint import install_fake
from store_project.meso.tests.test_agent_endpoint import one_swap_result
from store_project.meso.tests.test_agent_endpoint import propose
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.meso.tests.test_athlete_logging import log_url
from store_project.meso.tests.test_athlete_logging import seed as log_seed
from store_project.meso.tests.test_batch_deliver import comp
from store_project.meso.tests.test_batch_deliver import seed_source
from store_project.meso.tests.test_billing_enforcement import _plan_with_prescription
from store_project.meso.tests.test_billing_stripe import _coach_with_customer
from store_project.meso.tests.test_billing_stripe import _invoice_event
from store_project.meso.tests.test_billing_stripe import _sub_event
from store_project.meso.tests.test_deliver import deliver_url
from store_project.meso.tests.test_deliver import seed_plan as deliver_seed_plan
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed as cell_seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.meso.tests.test_plan_create import _aged_link
from store_project.meso.tests.test_plan_create import _plan_new_url
from store_project.meso.tests.test_plan_draft import DraftingClient
from store_project.meso.tests.test_plan_draft import _install
from store_project.meso.tests.test_push import SUBSCRIBE
from store_project.meso.tests.test_push import sub_body
from store_project.meso.tests.test_requests import make_coach
from store_project.meso.tests.test_settle import quiet_since
from store_project.meso.tests.test_settle import set_activity
from store_project.meso.tests.test_template_library import coach_with_client
from store_project.meso.tests.test_template_library import template_plan
from store_project.meso.tests.test_template_library import use_url as template_use_url
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def events(name):
    """Every ``Event`` row of ``name``, oldest first (deterministic order)."""
    return list(Event.objects.filter(name=name).order_by("id"))


def sets_payload(presc, *pairs):
    """The structured logger's ``sets`` body from a list of (reps, load) pairs."""
    return [
        {
            "prescription": presc.pk,
            "set_number": n,
            "reps": reps,
            "load": load,
            "rpe": "8",
        }
        for n, (reps, load) in enumerate(pairs, start=1)
    ]


def post_log(client, session, payload):
    return client.post(
        log_url(session), data=json.dumps(payload), content_type="application/json"
    )


# ---------------------------------------------------------------------------
# plan_created
# ---------------------------------------------------------------------------


class TestPlanCreatedEvent:
    def test_new_plan_gives_one_event(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete))

        plan = Plan.objects.get(relationship=link)
        rows = events(EventName.PLAN_CREATED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == link.coach
        assert row.subject_type == plan._meta.label_lower
        assert row.subject_id == str(plan.pk)
        assert row.props == {"athlete": str(link.athlete_id), "draft": False}

    def test_reposting_for_an_existing_plan_gives_no_second_event(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete))
        client.post(_plan_new_url(link.athlete))

        assert len(events(EventName.PLAN_CREATED)) == 1

    def test_suspended_athlete_gives_no_event(self, client):
        coach = UserFactory()
        _aged_link(coach, days_ago=30)  # oldest → kept
        suspended = _aged_link(coach, days_ago=1)  # newest → over cap → suspended
        client.force_login(coach)

        client.post(_plan_new_url(suspended.athlete))

        assert events(EventName.PLAN_CREATED) == []


# ---------------------------------------------------------------------------
# agent_proposal_run
# ---------------------------------------------------------------------------


class TestAgentProposalRunEventOnDraft:
    def test_draft_with_ai_gives_plan_created_and_one_agent_proposal_run(
        self, client, monkeypatch
    ):
        link = CoachAthleteFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete), data={"draft": "agent"})

        plan = link.working_plan()
        batch = plan.proposal_batches.get()

        plan_rows = events(EventName.PLAN_CREATED)
        assert len(plan_rows) == 1
        assert plan_rows[0].props == {"athlete": str(link.athlete_id), "draft": True}

        agent_rows = events(EventName.AGENT_PROPOSAL_RUN)
        assert len(agent_rows) == 1
        row = agent_rows[0]
        assert row.actor == link.coach
        assert row.subject_type == batch._meta.label_lower
        assert row.subject_id == str(batch.pk)
        assert row.props == {"trigger": AgentProposalBatch.Trigger.DRAFT}

    def test_exhausted_allowance_gives_no_agent_event(self, client, monkeypatch):
        link = CoachAthleteFactory()
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(coach=link.coach)
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete), data={"draft": "agent"})

        assert events(EventName.AGENT_PROPOSAL_RUN) == []

    def test_no_api_key_gives_no_agent_event(self, client, monkeypatch):
        link = CoachAthleteFactory()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete), data={"draft": "agent"})

        assert events(EventName.AGENT_PROPOSAL_RUN) == []


class TestAgentProposalRunEventOnManualPropose:
    def test_successful_propose_gives_one_event(self, client, monkeypatch):
        plan, _, presc = make_plan()
        install_fake(monkeypatch, one_swap_result(presc))
        client.force_login(plan.coach)

        resp = propose(client, plan)
        assert resp.status_code == 202

        batch = AgentProposalBatch.objects.get()
        rows = events(EventName.AGENT_PROPOSAL_RUN)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == plan.coach
        assert row.subject_type == batch._meta.label_lower
        assert row.subject_id == str(batch.pk)
        assert row.props == {"trigger": AgentProposalBatch.Trigger.MANUAL}

    def test_over_allowance_402_gives_no_event(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(plan=plan, coach=coach)
        client.force_login(coach)

        resp = propose(client, plan)

        assert resp.status_code == 402
        assert events(EventName.AGENT_PROPOSAL_RUN) == []

    def test_no_api_key_503_gives_no_event(self, client, monkeypatch):
        plan, _, _ = make_plan()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        client.force_login(plan.coach)

        resp = propose(client, plan)

        assert resp.status_code == 503
        assert events(EventName.AGENT_PROPOSAL_RUN) == []

    def test_empty_instruction_400_gives_no_event(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(plan.coach)

        resp = client.post(
            agent_url(plan), data=json.dumps({}), content_type="application/json"
        )

        assert resp.status_code == 400
        assert events(EventName.AGENT_PROPOSAL_RUN) == []


# ---------------------------------------------------------------------------
# template_imported
# ---------------------------------------------------------------------------


class TestTemplateImportedEvent:
    def test_one_event_on_start_for_client(self, client):
        coach, rel = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        client.post(template_use_url(tpl), {"relationship": rel.pk})

        copy = rel.plans.get()
        rows = events(EventName.TEMPLATE_IMPORTED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == coach
        assert row.subject_type == copy._meta.label_lower
        assert row.subject_id == str(copy.pk)
        assert row.props == {"template": tpl.pk, "athlete": str(rel.athlete_id)}

    def test_invalid_relationship_gives_no_event(self, client):
        coach, _ = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        client.post(template_use_url(tpl), {"relationship": 999999})

        assert events(EventName.TEMPLATE_IMPORTED) == []


# ---------------------------------------------------------------------------
# batch_applied
# ---------------------------------------------------------------------------


class TestBatchAppliedEvent:
    def test_one_event_with_the_right_counts(self, client):
        plan, _, batch, _ = make_batch_with_swap()
        client.force_login(plan.coach)

        resp = client.post(apply_url(batch))
        data = resp.json()

        rows = events(EventName.BATCH_APPLIED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == plan.coach
        assert row.subject_type == batch._meta.label_lower
        assert row.subject_id == str(batch.pk)
        assert row.props == {"applied": data["applied"], "skipped": data["skipped"]}

    def test_second_apply_409_gives_no_second_event(self, client):
        plan, _, batch, _ = make_batch_with_swap()
        client.force_login(plan.coach)

        client.post(apply_url(batch))
        resp = client.post(apply_url(batch))

        assert resp.status_code == 409
        assert len(events(EventName.BATCH_APPLIED)) == 1


# ---------------------------------------------------------------------------
# block_delivered
# ---------------------------------------------------------------------------


class TestBlockDeliveredEvent:
    def test_deliver_gives_one_event_via_deliver(self, client):
        plan, week, _, _ = deliver_seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))
        assert resp.status_code == 201

        meso = week.mesocycle
        rows = events(EventName.BLOCK_DELIVERED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == plan.relationship.coach
        assert row.subject_type == meso._meta.label_lower
        assert row.subject_id == str(meso.pk)
        assert row.props == {
            "plan": plan.pk,
            "athlete": str(plan.athlete.pk),
            "weeks": 1,
            "via": "deliver",
        }

    def test_batch_deliver_to_two_clients_gives_two_events(self, client):
        plan, _ = seed_source(coach=comp(UserFactory()))
        rel_b = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        rel_c = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        client.force_login(plan.coach)

        resp = client.post(
            reverse("meso:plan_batch_deliver", kwargs={"plan_id": plan.pk}),
            {"relationships": [rel_b.pk, rel_c.pk]},
        )
        assert resp.status_code == 302

        rows = events(EventName.BLOCK_DELIVERED)
        assert len(rows) == 2
        assert all(r.props["via"] == "batch" for r in rows)
        copy_b = rel_b.plans.get()
        copy_c = rel_c.plans.get()
        subject_ids = {r.subject_id for r in rows}
        assert subject_ids == {
            str(copy_b.mesocycles.get().pk),
            str(copy_c.mesocycles.get().pk),
        }

    def test_template_deliver_400_gives_no_event(self, client):
        coach = UserFactory()
        tpl, _ = template_plan(coach, title="Base Block", with_grid=False)
        client.force_login(coach)

        resp = client.post(deliver_url(tpl))

        assert resp.status_code == 400
        assert events(EventName.BLOCK_DELIVERED) == []


# ---------------------------------------------------------------------------
# session_opened
# ---------------------------------------------------------------------------


class TestSessionOpenedEvent:
    def _url(self, session):
        return reverse("meso:athlete_session", kwargs={"pk": session.pk})

    def test_get_gives_one_event(self, client):
        s = log_seed()
        client.force_login(s.athlete)

        resp = client.get(self._url(s.session))

        assert resp.status_code == 200
        rows = events(EventName.SESSION_OPENED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == s.athlete
        assert row.subject_type == s.session._meta.label_lower
        assert row.subject_id == str(s.session.pk)
        assert row.props == {}

    def test_head_gives_no_event(self, client):
        s = log_seed()
        client.force_login(s.athlete)

        resp = client.head(self._url(s.session))

        assert resp.status_code == 200
        assert events(EventName.SESSION_OPENED) == []

    def test_foreign_session_404_gives_no_event(self, client):
        s = log_seed()
        intruder = log_seed().athlete
        client.force_login(intruder)

        resp = client.get(self._url(s.session))

        assert resp.status_code == 404
        assert events(EventName.SESSION_OPENED) == []


# ---------------------------------------------------------------------------
# set_logged — typed path (athlete_cell_write → _upsert_parsed_set)
# ---------------------------------------------------------------------------


class TestSetLoggedTypedEvent:
    def test_first_set_gives_one_event(self, client):
        s = cell_seed()
        client.force_login(s.athlete)

        write_cell(client, s.session, s.squat, 1, "225 x 5")

        log = the_log(s.session, s.athlete)
        rows = events(EventName.SET_LOGGED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == s.athlete
        assert row.subject_type == log._meta.label_lower
        assert row.subject_id == str(log.pk)
        assert row.props == {"via": "typed"}

    def test_reblur_same_text_gives_no_more(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert len(events(EventName.SET_LOGGED)) == 1

    def test_editing_the_values_gives_no_more(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        write_cell(client, s.session, s.squat, 1, "230 x 3")

        assert len(events(EventName.SET_LOGGED)) == 1

    def test_a_second_line_gives_one_more(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        write_cell(client, s.session, s.squat, 2, "225 x 5")

        assert len(events(EventName.SET_LOGGED)) == 2

    def test_blank_then_retype_gives_one_more(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 1, "")  # blank clears the cell

        write_cell(client, s.session, s.squat, 1, "225 x 5")  # typed again

        assert len(events(EventName.SET_LOGGED)) == 2


# ---------------------------------------------------------------------------
# set_logged — structured path (athlete_log_session)
# ---------------------------------------------------------------------------


class TestSetLoggedStructuredEvent:
    def test_first_save_with_three_sets_gives_three_events(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        payload = {
            "sets": sets_payload(s.squat, ("5", "225"), ("5", "225"), ("5", "225"))
        }

        resp = post_log(client, s.session, payload)
        assert resp.status_code == 200

        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        rows = events(EventName.SET_LOGGED)
        assert len(rows) == 3
        assert all(r.props == {"via": "log"} for r in rows)
        assert all(r.subject_id == str(log.pk) for r in rows)
        assert all(r.subject_type == log._meta.label_lower for r in rows)
        assert all(r.actor == s.athlete for r in rows)

    def test_identical_resave_gives_no_more(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        payload = {"sets": sets_payload(s.squat, ("5", "225"))}
        post_log(client, s.session, payload)

        post_log(client, s.session, payload)

        assert len(events(EventName.SET_LOGGED)) == 1

    def test_one_extra_set_gives_one_more(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        post_log(client, s.session, {"sets": sets_payload(s.squat, ("5", "225"))})

        post_log(
            client,
            s.session,
            {"sets": sets_payload(s.squat, ("5", "225"), ("5", "230"))},
        )

        assert len(events(EventName.SET_LOGGED)) == 2

    def test_reposting_a_typed_set_gives_no_more(self, client):
        # Type a line (typed path creates it), then "Log session" with that
        # exact set in the payload, the way the client would.
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert len(events(EventName.SET_LOGGED)) == 1
        cell = sub_cell(s.squat, 1)
        typed_row = LoggedSet.objects.get(source_line=cell)

        payload = {
            "sets": [
                {
                    "prescription": s.squat.pk,
                    "set_number": typed_row.set_number,
                    "reps": typed_row.reps,
                    "load": typed_row.load,
                    "rpe": typed_row.rpe,
                }
            ]
        }
        resp = log_post(client, s.session, payload)

        assert resp.status_code == 200
        assert len(events(EventName.SET_LOGGED)) == 1


# ---------------------------------------------------------------------------
# session_completed — structured "log" path
# ---------------------------------------------------------------------------


class TestSessionCompletedLogEvent:
    def test_log_session_gives_one_event(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        payload = {"sets": sets_payload(s.squat, ("5", "225"))}  # status defaults done

        resp = post_log(client, s.session, payload)
        assert resp.status_code == 200

        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        rows = events(EventName.SESSION_COMPLETED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == s.athlete
        assert row.subject_type == log._meta.label_lower
        assert row.subject_id == str(log.pk)
        assert row.props == {"via": "log"}

    def test_repeat_log_session_gives_no_more(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        payload = {"sets": sets_payload(s.squat, ("5", "225"))}
        post_log(client, s.session, payload)

        post_log(client, s.session, payload)

        assert len(events(EventName.SESSION_COMPLETED)) == 1

    def test_pending_save_gives_no_event_then_done_gives_one(self, client):
        s = log_seed()
        client.force_login(s.athlete)
        pending = {
            "status": "pending",
            "sets": sets_payload(s.squat, ("5", "225")),
        }

        post_log(client, s.session, pending)
        assert events(EventName.SESSION_COMPLETED) == []

        done = {"status": "done", "sets": sets_payload(s.squat, ("5", "225"))}
        post_log(client, s.session, done)

        assert len(events(EventName.SESSION_COMPLETED)) == 1


# ---------------------------------------------------------------------------
# session_completed — settle path
# ---------------------------------------------------------------------------


class TestSessionCompletedSettleEvent:
    def _cutoff(self):
        return timezone.now() - settle.quiet_period()

    def test_quiet_pending_log_with_a_set_settles_and_tracks(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())

        settled = settle.settle_log(log.pk, cutoff=self._cutoff())

        assert settled is True
        rows = events(EventName.SESSION_COMPLETED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == s.athlete
        assert row.subject_type == log._meta.label_lower
        assert row.subject_id == str(log.pk)
        assert row.props == {"via": "settle"}

    def test_too_recent_declines_and_gives_no_event(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")  # last_activity_at ~ now
        log = the_log(s.session, s.athlete)

        settled = settle.settle_log(log.pk, cutoff=self._cutoff())

        assert settled is False
        assert events(EventName.SESSION_COMPLETED) == []

    def test_already_done_declines_and_gives_no_event(self, client):
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        SessionLog.objects.filter(pk=log.pk).update(status=SessionLog.Status.DONE)

        settled = settle.settle_log(log.pk, cutoff=self._cutoff())

        assert settled is False
        assert events(EventName.SESSION_COMPLETED) == []

    def test_no_sets_declines_and_gives_no_event(self, client):
        s = cell_seed()
        log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        set_activity(log, quiet_since())

        settled = settle.settle_log(log.pk, cutoff=self._cutoff())

        assert settled is False
        assert events(EventName.SESSION_COMPLETED) == []

    def test_settled_log_then_log_session_gives_no_more(self, client):
        # DONE is sticky (5b): once settled, a later "Log session" must not
        # fire a second session_completed.
        s = cell_seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        log = the_log(s.session, s.athlete)
        set_activity(log, quiet_since())
        assert settle.settle_log(log.pk, cutoff=self._cutoff()) is True
        assert len(events(EventName.SESSION_COMPLETED)) == 1

        resp = log_post(client, s.session, {"sets": []})

        assert resp.status_code == 200
        assert len(events(EventName.SESSION_COMPLETED)) == 1


# ---------------------------------------------------------------------------
# invite_sent
# ---------------------------------------------------------------------------


class TestInviteSentEvent:
    def test_first_invite_gives_one_event_new_true(self, client):
        coach = UserFactory()
        client.force_login(coach)

        client.post(reverse("meso:coach_invite"), {"email": "ath@example.com"})

        invite = CoachInvite.objects.get(coach=coach)
        rows = events(EventName.INVITE_SENT)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == coach
        assert row.subject_type == invite._meta.label_lower
        assert row.subject_id == str(invite.pk)
        assert row.props == {"new": True}

    def test_reinvite_gives_a_second_event_new_false(self, client):
        coach = UserFactory()
        client.force_login(coach)
        client.post(reverse("meso:coach_invite"), {"email": "ath@example.com"})

        client.post(reverse("meso:coach_invite"), {"email": "ath@example.com"})

        rows = events(EventName.INVITE_SENT)
        assert len(rows) == 2
        assert [r.props["new"] for r in rows] == [True, False]

    def test_self_invite_gives_no_event(self, client):
        coach = UserFactory(email="coach@example.com")
        client.force_login(coach)

        client.post(reverse("meso:coach_invite"), {"email": "Coach@example.com"})

        assert events(EventName.INVITE_SENT) == []

    def test_invalid_email_gives_no_event(self, client):
        coach = UserFactory()
        client.force_login(coach)

        client.post(reverse("meso:coach_invite"), {"email": "not-an-email"})

        assert events(EventName.INVITE_SENT) == []


# ---------------------------------------------------------------------------
# invite_accepted
# ---------------------------------------------------------------------------


class TestInviteAcceptedEvent:
    def _claim_url(self, invite):
        return reverse("meso:invite_claim", kwargs={"token": invite.token})

    def test_accept_gives_one_event(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        client.force_login(athlete)

        resp = client.post(self._claim_url(invite), {"action": "accept"})

        assert resp.status_code == 302
        rows = events(EventName.INVITE_ACCEPTED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == athlete
        assert row.subject_type == invite._meta.label_lower
        assert row.subject_id == str(invite.pk)
        assert row.props == {}

    def test_decline_gives_no_event(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        client.force_login(athlete)

        client.post(self._claim_url(invite), {"action": "decline"})

        assert events(EventName.INVITE_ACCEPTED) == []

    def test_already_answered_gives_no_event(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        invite.revoke()
        client.force_login(athlete)

        client.post(self._claim_url(invite), {"action": "accept"})

        assert events(EventName.INVITE_ACCEPTED) == []


# ---------------------------------------------------------------------------
# coach_request_sent
# ---------------------------------------------------------------------------


class TestCoachRequestSentEvent:
    def test_request_gives_one_event(self, client):
        coach = make_coach(email="coach@example.com")
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.post(
            reverse("meso:athlete_request_coach"), {"email": "coach@example.com"}
        )

        assert resp.status_code == 302
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        rows = events(EventName.COACH_REQUEST_SENT)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == athlete
        assert row.subject_type == link._meta.label_lower
        assert row.subject_id == str(link.pk)
        assert row.props == {}

    def test_already_pending_gives_no_second_event(self, client):
        coach = make_coach()
        athlete = UserFactory()
        client.force_login(athlete)
        client.post(reverse("meso:athlete_request_coach"), {"email": coach.email})

        client.post(reverse("meso:athlete_request_coach"), {"email": coach.email})

        assert len(events(EventName.COACH_REQUEST_SENT)) == 1

    def test_unknown_coach_gives_no_event(self, client):
        athlete = UserFactory()
        client.force_login(athlete)

        client.post(
            reverse("meso:athlete_request_coach"), {"email": "nobody@example.com"}
        )

        assert events(EventName.COACH_REQUEST_SENT) == []


# ---------------------------------------------------------------------------
# subscription_started — local trial
# ---------------------------------------------------------------------------


class TestSubscriptionStartedTrialEvent:
    def test_billing_start_trial_gives_one_event(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        client.post(reverse("meso:billing_start_trial"))

        sub = CoachSubscription.objects.get(coach=coach)
        rows = events(EventName.SUBSCRIPTION_STARTED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == coach
        assert row.subject_type == sub._meta.label_lower
        assert row.subject_id == str(sub.pk)
        assert row.props == {
            "via": "trial",
            "status": CoachSubscription.Status.TRIALING,
        }

    def test_second_trial_attempt_gives_no_second_event(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        client.post(reverse("meso:billing_start_trial"))

        client.post(reverse("meso:billing_start_trial"))

        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_start_coaching_with_trial_plan_gives_one_event(self, client):
        user = UserFactory()
        client.force_login(user)

        client.post(reverse("meso:start_coaching"), data={"plan": "trial"})

        rows = events(EventName.SUBSCRIPTION_STARTED)
        assert len(rows) == 1
        assert rows[0].props == {
            "via": "trial",
            "status": CoachSubscription.Status.TRIALING,
        }


# ---------------------------------------------------------------------------
# subscription_started / subscription_cancelled — Stripe
# ---------------------------------------------------------------------------


class TestSubscriptionStripeEvents:
    def test_created_active_gives_one_started(self):
        coach = _coach_with_customer()

        billing_webhooks.handle_event(_sub_event("customer.subscription.created"))

        sub = CoachSubscription.objects.get(coach=coach)
        rows = events(EventName.SUBSCRIPTION_STARTED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == coach
        assert row.subject_type == sub._meta.label_lower
        assert row.subject_id == str(sub.pk)
        assert row.props == {
            "via": "stripe",
            "subscription": "sub_1",
            "status": CoachSubscription.Status.ACTIVE,
            "previous": "",
        }

    def test_duplicate_retried_active_gives_no_second_started(self):
        _coach_with_customer()
        event = _sub_event("customer.subscription.updated", item_id="si_x")
        billing_webhooks.handle_event(event)

        billing_webhooks.handle_event(event)

        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_trial_then_stripe_active_gives_one_started(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.TRIALING)

        billing_webhooks.handle_event(_sub_event("customer.subscription.created"))

        rows = events(EventName.SUBSCRIPTION_STARTED)
        assert len(rows) == 1
        assert rows[0].props["previous"] == CoachSubscription.Status.TRIALING

    def test_deleted_gives_one_cancelled(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )

        sub = CoachSubscription.objects.get(coach=coach)
        rows = events(EventName.SUBSCRIPTION_CANCELLED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == coach
        assert row.subject_type == sub._meta.label_lower
        assert row.subject_id == str(sub.pk)
        assert row.props == {
            "via": "stripe",
            "subscription": "sub_1",
            "status": CoachSubscription.Status.CANCELED,
            "previous": CoachSubscription.Status.ACTIVE,
            "reason": "",
        }

    def test_repeated_deleted_gives_no_more_cancelled(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )

        assert len(events(EventName.SUBSCRIPTION_CANCELLED)) == 1

    def test_unknown_customer_gives_no_events(self):
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", customer="cus_nobody")
        )

        assert events(EventName.SUBSCRIPTION_STARTED) == []
        assert events(EventName.SUBSCRIPTION_CANCELLED) == []

    def test_stale_event_for_a_different_subscription_gives_no_events(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_new",
            stripe_item_id="si_new",
        )

        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.updated", sub_id="sub_old", status="active"
            )
        )

        assert events(EventName.SUBSCRIPTION_STARTED) == []
        assert events(EventName.SUBSCRIPTION_CANCELLED) == []

    def test_incomplete_then_invoice_paid_then_updated_active_gives_one_started_at_paid(
        self,
    ):
        """Incomplete never counts as live.

        So the ledger — not the mirror's ``already_live`` — must catch the
        later ``updated(active)`` as a dup (a).
        """
        _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="incomplete")
        )
        assert events(EventName.SUBSCRIPTION_STARTED) == []

        billing_webhooks.handle_event(_invoice_event("invoice.paid"))
        rows = events(EventName.SUBSCRIPTION_STARTED)
        assert len(rows) == 1
        assert rows[0].props["previous"] == CoachSubscription.Status.PAST_DUE

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="active")
        )
        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_incomplete_then_updated_active_then_invoice_paid_gives_one_started(self):
        _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="incomplete")
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="active")
        )
        billing_webhooks.handle_event(_invoice_event("invoice.paid"))

        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_incomplete_then_deleted_gives_no_started_or_cancelled(self):
        """(b): a subscription that never went live must not get a cancelled row."""
        _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="incomplete")
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )

        assert events(EventName.SUBSCRIPTION_STARTED) == []
        assert events(EventName.SUBSCRIPTION_CANCELLED) == []

    def test_incomplete_then_incomplete_expired_gives_no_cancelled(self):
        _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="incomplete")
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="incomplete_expired")
        )

        assert events(EventName.SUBSCRIPTION_CANCELLED) == []

    def test_active_then_payment_failed_then_paid_recovery_gives_one_started(self):
        _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="active")
        )
        billing_webhooks.handle_event(_invoice_event("invoice.payment_failed"))
        billing_webhooks.handle_event(_invoice_event("invoice.paid"))

        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_active_then_deleted_then_late_retried_active_does_not_double_start(self):
        """A late retried ``updated(active)`` resurrects the mirror.

        But it must not write a second ``subscription_started`` — the ledger
        already has one.
        """
        coach = _coach_with_customer()

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", status="active")
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="active")
        )

        assert len(events(EventName.SUBSCRIPTION_STARTED)) == 1
        assert len(events(EventName.SUBSCRIPTION_CANCELLED)) == 1
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE  # the mirror resurrects

    def test_pre_existing_live_subscription_renewal_gives_no_started(self):
        """A mirror row already live before the ledger existed (pre-deploy).

        It must stay quiet on a renewal update — ``already_live`` alone
        decides this.
        """
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="active")
        )
        assert events(EventName.SUBSCRIPTION_STARTED) == []

        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )
        assert len(events(EventName.SUBSCRIPTION_CANCELLED)) == 1

    def test_cancelled_carries_the_reason_from_cancellation_details(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )

        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.deleted",
                status="canceled",
                cancellation_details={"reason": "cancellation_requested"},
            )
        )

        row = events(EventName.SUBSCRIPTION_CANCELLED)[0]
        assert row.props["reason"] == "cancellation_requested"


# ---------------------------------------------------------------------------
# push_subscribed
# ---------------------------------------------------------------------------


class TestPushSubscribedEvent:
    def test_first_post_gives_one_event(self, client):
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")

        assert resp.status_code == 201
        sub = PushSubscription.objects.get()
        rows = events(EventName.PUSH_SUBSCRIBED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == athlete
        assert row.subject_type == sub._meta.label_lower
        assert row.subject_id == str(sub.pk)
        assert row.props == {}

    def test_identical_repost_gives_no_more(self, client):
        athlete = UserFactory()
        client.force_login(athlete)
        client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")

        client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")

        assert len(events(EventName.PUSH_SUBSCRIBED)) == 1

    def test_same_endpoint_different_user_gives_one_more(self, client):
        first = UserFactory()
        client.force_login(first)
        client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")

        second = UserFactory()
        client.force_login(second)
        client.post(SUBSCRIBE, data=sub_body(), content_type="application/json")

        assert len(events(EventName.PUSH_SUBSCRIBED)) == 2

    def test_malformed_body_gives_no_event(self, client):
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.post(SUBSCRIBE, data="{not json", content_type="application/json")

        assert resp.status_code == 400
        assert events(EventName.PUSH_SUBSCRIBED) == []
