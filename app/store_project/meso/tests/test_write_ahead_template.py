"""#643 — the write-ahead-as-template path follows through to the accept.

A coach with an invite pending writes the program as a template. The template
remembers the invite (``Plan.for_invite``); when the athlete accepts, the coach
is emailed and the athlete's roster row offers one-click "Start <title>".
"""

import re
from unittest import mock

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.meso.models import Unit
from store_project.users.factories import UserFactory

from .test_template_plans import template_plan

pytestmark = pytest.mark.django_db

ACTIVE = CoachAthlete.Status.ACTIVE
WAITING = CoachAthlete.Status.ACCEPTED_WAITING


def make_coach(email="sam@example.com"):
    coach = UserFactory(name="Sam Coach", email=email)
    CoachProfileFactory(
        user=coach, display_name="Sam Coach", default_unit=Unit.KILOGRAMS
    )
    return coach


def invited(coach, *, label="Jordan Ellis"):
    invite, _ = CoachInvite.open_for(
        coach=coach, email="jordan@example.com", label=label
    )
    return invite


def written_template(coach, invite, title="Strength Foundations"):
    tpl, _ = template_plan(coach, title=title)
    tpl.for_invite = invite
    tpl.save(update_fields=["for_invite"])
    return tpl


def claim(client, invite, athlete, capture):
    client.force_login(athlete)
    url = reverse("meso:invite_claim", kwargs={"token": invite.token})
    with capture(execute=True):
        return client.post(url, {"action": "accept"})


class TestWriteItAsATemplate:
    def test_post_with_pending_token_opens_new_template_for_invite(self, client):
        coach = make_coach()
        invite = invited(coach)
        client.force_login(coach)

        resp = client.post(reverse("meso:template_create"), {"invite": invite.token})

        tpl = Plan.objects.get(is_template=True, owner=coach)
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": tpl.pk})
        assert resp.url != reverse("meso:template_library")
        assert tpl.for_invite == invite

    def test_foreign_token_is_ignored(self, client):
        coach = make_coach()
        other_invite = invited(make_coach("other@example.com"))
        client.force_login(coach)

        resp = client.post(
            reverse("meso:template_create"), {"invite": other_invite.token}
        )

        tpl = Plan.objects.get(is_template=True, owner=coach)
        assert resp.status_code == 302
        assert tpl.for_invite is None

    def test_non_pending_and_garbage_tokens_are_ignored(self, client):
        coach = make_coach()
        invite = invited(coach)
        invite.revoke()
        client.force_login(coach)

        client.post(reverse("meso:template_create"), {"invite": invite.token})
        client.post(reverse("meso:template_create"), {"invite": "not-a-uuid"})

        tpls = Plan.objects.filter(is_template=True, owner=coach)
        assert tpls.count() == 2
        assert not tpls.exclude(for_invite=None).exists()

    def test_roster_renders_post_form_with_the_single_pending_token(self, client):
        coach = make_coach()
        invite = invited(coach)
        client.force_login(coach)

        html = client.get(reverse("meso:roster")).content.decode()

        form = re.search(
            r'<form[^>]*action="%s"[^>]*>(.*?)</form>'
            % re.escape(reverse("meso:template_create")),
            html,
            re.S,
        )
        assert form, "Write it as a template must be a POST form"
        assert 'method="post"' in form.group(0)
        assert "csrfmiddlewaretoken" in form.group(1)
        assert f'name="invite" value="{invite.token}"' in form.group(1)
        assert "Write it as a template" in form.group(1)

    def test_roster_form_carries_no_token_with_several_pending(self, client):
        coach = make_coach()
        invited(coach)
        CoachInvite.open_for(coach=coach, email="two@example.com", label="Two")
        client.force_login(coach)

        html = client.get(reverse("meso:roster")).content.decode()

        assert "Write it as a template" in html
        assert 'name="invite"' not in html


class TestCoachEmailedOnAccept:
    def test_with_template_names_it(self, client, django_capture_on_commit_callbacks):
        coach = make_coach()
        invite = invited(coach)
        written_template(coach, invite)

        claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )

        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == [coach.email]
        assert (
            msg.subject
            == "Jordan Ellis accepted your invite — Strength Foundations is ready to start."
        )
        assert reverse("meso:roster") in msg.body

    def test_without_template(self, client, django_capture_on_commit_callbacks):
        coach = make_coach()
        invite = invited(coach)

        claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )

        assert [m.subject for m in mail.outbox] == [
            "Jordan Ellis accepted your invite."
        ]
        assert reverse("meso:roster") in mail.outbox[0].body

    def test_archived_template_is_not_named(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        invite = invited(coach)
        tpl = written_template(coach, invite)
        Plan.objects.filter(pk=tpl.pk).update(status=Plan.Status.ARCHIVED)

        claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )

        assert [m.subject for m in mail.outbox] == [
            "Jordan Ellis accepted your invite."
        ]

    def test_auto_accept_after_signup_flow(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        invite = invited(coach)
        written_template(coach, invite)
        url = reverse("meso:invite_claim", kwargs={"token": invite.token})
        client.get(url)  # anonymous landing remembers the claim in the session
        athlete = UserFactory(name="Jordan Ellis")
        athlete.set_password("pw-12345-xyz")
        athlete.save()
        # A real allauth login back to the claim page keeps the flag; a bare
        # ``force_login`` is a non-claim-bound login and clears it (#677).
        client.post(
            reverse("account_login"),
            {"login": athlete.email, "password": "pw-12345-xyz", "next": url},
        )

        with django_capture_on_commit_callbacks(execute=True):
            client.get(url)

        assert CoachAthlete.objects.get(coach=coach, athlete=athlete).status == ACTIVE
        assert len(mail.outbox) == 1
        assert "Strength Foundations is ready to start" in mail.outbox[0].subject

    def test_sandbox_coach_gets_none(self, client, django_capture_on_commit_callbacks):
        coach = make_coach()
        invite = invited(coach)
        with mock.patch(
            "store_project.meso.views.meso_sandbox.is_sandbox",
            side_effect=lambda user: user.pk == coach.pk,
        ):
            claim(
                client,
                invite,
                UserFactory(name="Jordan Ellis"),
                django_capture_on_commit_callbacks,
            )
        assert mail.outbox == []

    def test_coach_without_email_is_skipped(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach(email="")
        invite = invited(coach)
        resp = claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )
        assert resp.status_code == 302
        assert mail.outbox == []

    def test_waiting_case_sends_only_the_waiting_email(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=ACTIVE)
        invite = invited(coach)
        written_template(coach, invite)

        claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )

        assert [m.subject for m in mail.outbox] == [
            "Jordan Ellis accepted your invite. Upgrade to start coaching them."
        ]

    def test_peer_invite_accepted_by_athlete_emails_coach(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        athlete = UserFactory(name="Jordan Ellis")
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
            invited_by=CoachAthlete.InvitedBy.COACH,
        )
        client.force_login(athlete)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))

        link.refresh_from_db()
        assert link.status == ACTIVE
        assert [m.subject for m in mail.outbox] == [
            "Jordan Ellis accepted your invite."
        ]
        assert mail.outbox[0].to == [coach.email]

    def test_coach_accepting_an_athlete_request_sends_nothing(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        athlete = UserFactory(name="Jordan Ellis")
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
        )
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))

        link.refresh_from_db()
        assert link.status == ACTIVE
        assert mail.outbox == []


def accepted(coach, invite, athlete):
    """Materialize the invite as an active link, the way a claim does."""
    return invite.accept(athlete)


class TestRosterStartButton:
    def setup_method(self):
        self.coach = make_coach()
        self.invite = invited(self.coach)
        self.tpl = written_template(self.coach, self.invite)
        self.jordan = UserFactory(name="Jordan Ellis")
        self.link = accepted(self.coach, self.invite, self.jordan)
        self.use_url = reverse("meso:template_use", kwargs={"plan_id": self.tpl.pk})

    def roster(self, client):
        client.force_login(self.coach)
        return client.get(reverse("meso:roster")).content.decode()

    def test_row_offers_start_form_posting_to_template_use(self, client):
        html = self.roster(client)

        form = re.search(
            r'<form[^>]*action="%s"[^>]*>(.*?)</form>' % re.escape(self.use_url),
            html,
            re.S,
        )
        assert form, "expected a Start form posting to template_use"
        assert f'name="relationship" value="{self.link.pk}"' in form.group(1)
        assert "Start Strength Foundations" in form.group(1)
        assert "csrfmiddlewaretoken" in form.group(1)

    def test_posting_it_copies_and_opens_the_designer(self, client):
        client.force_login(self.coach)

        resp = client.post(self.use_url, {"relationship": self.link.pk})

        copy = self.link.plans.get()
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": copy.pk})
        assert copy.relationship == self.link
        assert not copy.is_template

    def test_unit_mismatch_warns_then_confirms(self, client):
        Plan.objects.filter(pk=self.tpl.pk).update(unit=Unit.POUNDS)
        client.force_login(self.coach)

        warn = client.post(self.use_url, {"relationship": self.link.pk})
        assert warn.status_code == 200
        assert self.link.plans.count() == 0
        html = warn.content.decode()
        assert 'name="relationship" value="%s"' % self.link.pk in html
        assert 'name="confirm_unit" value="1"' in html

        done = client.post(
            self.use_url, {"relationship": self.link.pk, "confirm_unit": "1"}
        )
        copy = self.link.plans.get()
        assert done.status_code == 302
        assert done.url == reverse("meso:designer_plan", kwargs={"plan_id": copy.pk})

    def test_button_gone_once_athlete_has_a_plan(self, client):
        PlanFactory(relationship=self.link, status=Plan.Status.ACTIVE)
        assert "Start Strength Foundations" not in self.roster(client)

    def test_archived_plan_does_not_hide_button(self, client):
        PlanFactory(relationship=self.link, status=Plan.Status.ARCHIVED)
        assert "Start Strength Foundations" in self.roster(client)

    def test_no_button_on_a_suspended_row(self, client):
        with mock.patch(
            "store_project.meso.views.billing_access.suspended_athlete_ids",
            return_value={self.link.pk},
        ):
            client.force_login(self.coach)
            html = client.get(reverse("meso:roster")).content.decode()
        assert "Jordan Ellis" in html
        assert "Start Strength Foundations" not in html

    def test_no_button_without_a_written_template(self, client):
        Plan.objects.filter(pk=self.tpl.pk).update(for_invite=None)
        assert "Start Strength Foundations" not in self.roster(client)

    def test_start_mapping_costs_one_query_for_any_number_of_athletes(self, client):
        CoachSubscription.objects.create(
            coach=self.coach, status=CoachSubscription.Status.COMPED
        )
        for i in range(4):
            invite = CoachInvite.open_for(
                coach=self.coach, email=f"x{i}@example.com", label=f"X{i}"
            )[0]
            written_template(self.coach, invite, title=f"T{i}")
            accepted(self.coach, invite, UserFactory(name=f"Athlete {i}"))
        client.force_login(self.coach)
        url = reverse("meso:roster")
        client.get(url)  # warm caches

        with CaptureQueriesContext(connection) as ctx:
            html = client.get(url).content.decode()

        assert len(re.findall(r">Start (?:Strength Foundations|T\d)<", html)) == 5
        # 5 athletes, 5 buttons: the invite→template mapping is ONE query.
        mapping = [
            q
            for q in ctx.captured_queries
            if "for_invite__accepted_link_id" in q["sql"]
        ]
        assert len(mapping) == 1


class TestReusedLink:
    def test_newest_invite_template_wins_on_the_roster(self, client):
        coach = make_coach()
        jordan = UserFactory(name="Jordan Ellis", email="jordan@example.com")
        i1 = invited(coach)
        written_template(coach, i1, title="Old")
        link = accepted(coach, i1, jordan)
        link.end(by="coach")
        i2 = invited(coach)
        assert i2.pk != i1.pk
        written_template(coach, i2, title="New")
        link2 = accepted(coach, i2, jordan)
        assert link2.pk == link.pk

        client.force_login(coach)
        html = client.get(reverse("meso:roster")).content.decode()

        assert "Start New" in html
        assert "Start Old" not in html


class TestMultilineTemplateTitle:
    def test_newline_title_still_emails_with_single_line_subject(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        invite = invited(coach)
        tpl = written_template(coach, invite)
        Plan.objects.filter(pk=tpl.pk).update(title="Strength\nFoundations")

        claim(
            client,
            invite,
            UserFactory(name="Jordan Ellis"),
            django_capture_on_commit_callbacks,
        )

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [coach.email]
        assert "\n" not in mail.outbox[0].subject
        assert "\r" not in mail.outbox[0].subject


class TestRosterClaimsPendingOnce:
    """#688.6: the "hasn't accepted" claim is stated once, by the explanation."""

    def _html(self, client, coach):
        client.force_login(coach)
        return client.get(reverse("meso:roster")).content.decode()

    def test_single_pending_invite_states_it_once(self, client):
        coach = make_coach()
        invited(coach)
        html = self._html(client, coach).replace("&#x27;", "'")
        assert html.count("hasn't accepted your invite yet") == 1
        assert "Write it as a template" in html

    def test_several_pending_invites_state_it_once(self, client):
        coach = make_coach()
        invited(coach)
        CoachInvite.open_for(coach=coach, email="two@example.com", label="Two")
        html = self._html(client, coach)
        assert html.count("invites has been accepted yet") == 1
