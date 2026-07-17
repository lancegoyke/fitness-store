"""Slice 3b (spreadsheet parity §3.4 / §3.1) — template library + new-from-template.

A template = a ``Plan`` with ``is_template=True``, no relationship, and an
``owner`` (see ``test_template_plans``). This slice adds the coach-facing UI on
top of that model:

- ``meso:template_library`` (``templates/``): the owner's library — every
  template they own, alphabetical, each opening in the designer, each offering
  "Start for client" + "Batch deliver". Scoped to the requester; login-gated.
- ``meso:template_use`` (``template/<plan_id>/use/``, POST): "Start for client"
  — deep-copies the template into a fresh, ACTIVE, *undelivered* client plan for
  one of the coach's active relationships, then opens it in the designer.
- ``meso:plan_batch_deliver`` from a template now redirects to the library (a
  template has no deliver screen to return to); from a normal plan it still
  redirects to the deliver screen (regression guard).

RED-phase spec tests: these fail until 3b is implemented (NoReverseMatch on the
new URL names / missing views), not on setup.
"""

import pytest
from django.core import mail
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import Plan
from store_project.meso.models import WeekDelivery
from store_project.users.factories import UserFactory

# Reuse the established fixture builders rather than re-deriving them.
from .test_batch_deliver import comp
from .test_batch_deliver import seed_source
from .test_template_plans import template_plan

pytestmark = pytest.mark.django_db


def coach_with_client():
    """A coach who counts as a coach (has one active client) — the library gate.

    ``RosterView`` (which the library mirrors) routes non-coaches to their
    athlete home, and ``_is_coach`` does NOT count template ownership alone, so
    every library viewer needs a coach-side link. The active client also makes
    the per-template "Start for client" / "Batch deliver" forms render.
    """
    coach = UserFactory()
    rel = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    return coach, rel


def library_url():
    return reverse("meso:template_library")


def use_url(plan):
    return reverse("meso:template_use", kwargs={"plan_id": plan.pk})


def batch_deliver_url(plan):
    return reverse("meso:plan_batch_deliver", kwargs={"plan_id": plan.pk})


class TestTemplateLibraryPage:
    def test_lists_owned_templates_linking_to_the_designer(self, client):
        coach, _ = coach_with_client()
        tpl_a, _ = template_plan(coach, title="Base Hypertrophy")
        tpl_b, _ = template_plan(coach, title="Peaking Block")
        client.force_login(coach)

        resp = client.get(library_url())

        assert resp.status_code == 200
        body = resp.content.decode()
        for tpl in (tpl_a, tpl_b):
            assert tpl.title in body
            assert reverse("meso:designer_plan", kwargs={"plan_id": tpl.pk}) in body

    def test_shows_only_the_requesters_templates(self, client):
        coach, rel = coach_with_client()
        mine, _ = template_plan(coach, title="My Template")
        # Another coach's template must not leak in.
        other, _ = template_plan(UserFactory(), title="Someone Elses Template")
        # The coach's own NON-template client plan must not appear either.
        client_plan = PlanFactory(relationship=rel, title="A Client Working Plan")
        client.force_login(coach)

        resp = client.get(library_url())

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "My Template" in body
        assert "Someone Elses Template" not in body
        assert "A Client Working Plan" not in body
        assert reverse("meso:designer_plan", kwargs={"plan_id": mine.pk}) in body
        assert reverse("meso:designer_plan", kwargs={"plan_id": other.pk}) not in body
        assert (
            reverse("meso:designer_plan", kwargs={"plan_id": client_plan.pk})
            not in body
        )

    def test_anonymous_is_redirected_to_login(self, client):
        # The library is a coach sub-surface (like DeliverView) — login-gated, so
        # an anonymous visitor is redirected, not shown the library.
        resp = client.get(library_url())
        assert resp.status_code == 302

    def test_empty_state_when_the_coach_has_no_templates(self, client):
        coach, _ = coach_with_client()
        client.force_login(coach)

        resp = client.get(library_url())

        assert resp.status_code == 200
        body = resp.content.decode()
        # Empty-state copy mentioning that templates can be imported. The
        # implementer must render this literal (or adjust the assertion to match).
        assert "No templates" in body

    def test_templates_listed_alphabetically_by_title(self, client):
        coach, _ = coach_with_client()
        template_plan(coach, title="601 Peak")
        template_plan(coach, title="101 Base")
        client.force_login(coach)

        resp = client.get(library_url())

        assert resp.status_code == 200
        body = resp.content.decode()
        assert body.find("101 Base") != -1
        assert body.find("601 Peak") != -1
        assert body.find("101 Base") < body.find("601 Peak")

    def test_roster_links_to_the_library(self, client):
        coach, _ = coach_with_client()
        client.force_login(coach)

        resp = client.get(reverse("meso:roster"))

        assert resp.status_code == 200
        assert library_url() in resp.content.decode()

    def test_each_template_offers_use_and_batch_deliver_forms(self, client):
        coach, _ = coach_with_client()  # active client → forms render
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.get(library_url())

        assert resp.status_code == 200
        body = resp.content.decode()
        assert use_url(tpl) in body
        assert batch_deliver_url(tpl) in body


class TestTemplateUseEndpoint:
    def test_starts_an_active_undelivered_copy_for_the_client(self, client):
        coach, rel = coach_with_client()
        tpl, cell = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": rel.pk})

        # Exactly one new client plan for that relationship.
        copy = rel.plans.get()
        assert Plan.objects.count() == 2  # template + the one copy
        # A normal client plan, not another template.
        assert copy.is_template is False
        assert copy.owner_id is None
        assert copy.relationship_id == rel.pk
        # A live, editable working plan (the status batch-deliver uses).
        assert copy.status == Plan.Status.ACTIVE
        # The deep copy carried the tree: same block count + the slot/cell content.
        assert copy.mesocycles.count() == tpl.mesocycles.count()
        copied_slot_names = list(
            copy.mesocycles.get()
            .session_slots.get()
            .exercise_slots.values_list("name", flat=True)
        )
        assert cell.exercise_slot.name in copied_slot_names
        assert copy.mesocycles.get().weeks.get().cells.filter(text=cell.text).exists()
        # Undelivered + unnotified: no week stamped, no snapshots, no email.
        copy_weeks = copy.mesocycles.get().weeks.all()
        assert all(w.delivered_at is None for w in copy_weeks)
        assert WeekDelivery.objects.filter(week__mesocycle__plan=copy).count() == 0
        assert len(mail.outbox) == 0
        # The template itself is untouched.
        tpl.refresh_from_db()
        assert tpl.is_template is True
        assert tpl.mesocycles.exists()
        # Opens the new copy in the designer.
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": copy.pk})

    def test_get_is_not_allowed(self, client):
        coach, _ = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        assert client.get(use_url(tpl)).status_code == 405

    def test_non_owner_coach_404s(self, client):
        tpl, _ = template_plan(title="Base Block")
        other_coach = CoachAthleteFactory().coach
        client.force_login(other_coach)

        resp = client.post(use_url(tpl), {"relationship": 1})

        assert resp.status_code == 404
        assert not tpl.mesocycles.filter(plan__is_template=False).exists()
        assert Plan.objects.filter(is_template=False).count() == 0

    def test_non_template_plan_404s(self, client):
        # The endpoint only serves templates.
        plan = PlanFactory()  # a normal relationship plan
        coach = plan.relationship.coach
        client.force_login(coach)

        resp = client.post(use_url(plan), {"relationship": plan.relationship.pk})

        assert resp.status_code == 404

    def test_relationship_of_a_different_coach_creates_nothing(self, client):
        coach, _ = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        foreign = CoachAthleteFactory()  # someone else's athlete
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": foreign.pk})

        assert resp.status_code == 302
        assert resp.url == library_url()
        assert foreign.plans.count() == 0
        assert Plan.objects.filter(is_template=False).count() == 0

    def test_missing_relationship_creates_nothing(self, client):
        coach, _ = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.post(use_url(tpl), {})

        assert resp.status_code == 302
        assert resp.url == library_url()
        assert Plan.objects.filter(is_template=False).count() == 0

    def test_garbage_relationship_creates_nothing(self, client):
        coach, _ = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": "not-an-int"})

        assert resp.status_code == 302
        assert resp.url == library_url()
        assert Plan.objects.filter(is_template=False).count() == 0

    def test_two_posts_create_two_independent_copies(self, client):
        coach, rel = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        client.post(use_url(tpl), {"relationship": rel.pk})
        client.post(use_url(tpl), {"relationship": rel.pk})

        assert rel.plans.count() == 2
        pks = set(rel.plans.values_list("pk", flat=True))
        assert len(pks) == 2  # two distinct plans


class TestBatchDeliverFromTemplate:
    def test_from_template_redirects_to_the_library(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = comp(UserFactory())
        tpl, _ = template_plan(coach, title="Squat Base")
        rel_b = CoachAthleteFactory(coach=coach, athlete=UserFactory())
        client.force_login(coach)

        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(batch_deliver_url(tpl), {"relationships": [rel_b.pk]})

        # Still creates + delivers an ACTIVE copy, exactly as before...
        copy = rel_b.plans.get()
        assert copy.status == Plan.Status.ACTIVE
        # ...but the redirect target is now the library (no template deliver screen).
        assert resp.status_code == 302
        assert resp.url == library_url()

    def test_from_normal_plan_still_redirects_to_the_deliver_screen(
        self, client, django_capture_on_commit_callbacks
    ):
        # Regression guard: batch-deliver of a normal plan is unchanged.
        plan, _ = seed_source(coach=comp(UserFactory()))
        rel_b = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        client.force_login(plan.coach)

        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(batch_deliver_url(plan), {"relationships": [rel_b.pk]})

        assert resp.status_code == 302
        assert resp.url == reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
