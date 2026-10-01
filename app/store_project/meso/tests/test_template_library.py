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

from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from store_project.meso.billing import access
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekDeliveryFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.models import WeekDelivery
from store_project.users.factories import UserFactory

# Reuse the established fixture builders rather than re-deriving them.
from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line
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


def _aged_link(coach, days_ago):
    """An active ``CoachAthlete`` for ``coach``, back-dated for a stable age.

    ``created_at`` is ``auto_now_add`` → a raw ``.update`` is the only way to set
    it; the oldest-kept suspension rule (D6) turns on this order.
    """
    link = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    link.refresh_from_db()
    return link


def _over_limit_coach():
    """A free coach over the seat cap (FREE_SEAT_LIMIT=1).

    The oldest link is kept live, the newest is soft-suspended (D6 freeze).
    """
    coach = UserFactory()
    kept = _aged_link(coach, days_ago=30)
    suspended = _aged_link(coach, days_ago=1)
    assert access.is_over_limit(coach) is True
    return coach, kept, suspended


def library_url():
    return reverse("meso:template_library")


def use_url(plan):
    return reverse("meso:template_use", kwargs={"plan_id": plan.pk})


def batch_deliver_url(plan):
    return reverse("meso:plan_batch_deliver", kwargs={"plan_id": plan.pk})


def save_as_template_url(plan):
    return reverse("meso:plan_save_as_template", kwargs={"plan_id": plan.pk})


def template_create_url():
    return reverse("meso:template_create")


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
        assert "meso_import_template" not in body
        assert "Save as template" in body
        assert template_create_url() in body

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

    def test_athlete_query_preselects_that_client(self, client):
        coach, rel = coach_with_client()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.get(f"{library_url()}?athlete={rel.pk}")

        assert resp.status_code == 200
        body = resp.content.decode()
        assert f"Starting a program for {rel.athlete.display_name()}" in body
        assert f'<option value="{rel.pk}" selected>' in body
        assert use_url(tpl) in body


class TestTemplateCreate:
    def test_creates_scaffolded_template_with_owner_default_unit(self, client):
        profile = CoachProfileFactory(default_unit=Unit.POUNDS)
        client.force_login(profile.user)

        resp = client.post(template_create_url())

        plan = Plan.objects.get(owner=profile.user, is_template=True)
        assert plan.relationship is None
        assert plan.status == Plan.Status.ACTIVE
        assert plan.unit == Unit.POUNDS
        assert plan.mesocycles.count() == 1
        assert plan.mesocycles.get().weeks.count() == 1
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})


class TestPlanSaveAsTemplate:
    def _live_plan(self):
        rel = CoachAthleteFactory()
        plan = PlanFactory(
            relationship=rel,
            title="Live Strength",
            goal="Peak",
            status=Plan.Status.DRAFT,
            unit=Unit.POUNDS,
        )
        meso = MesocycleFactory(plan=plan, name="Block A", order=2, week_count=4)
        week = WeekFactory(
            mesocycle=meso,
            index=1,
            phase="Accum",
            volume=72,
            intensity=81,
            is_deload=True,
        )
        session = day(week, day_number=3, name="Lower", bias="Squat", order=4)
        cell = presc(
            session,
            name="Back Squat",
            order=7,
            tags=["main", "barbell"],
            text="4 x 6, 225",
            tempo="301",
            rest="3m",
            note="Belt optional",
        )
        sub_line(cell, "RPE 8", line=1)
        return plan, cell

    def test_saves_full_program_tree_as_active_template(self, client):
        plan, cell = self._live_plan()
        client.force_login(plan.coach)

        resp = client.post(save_as_template_url(plan))

        copy = Plan.objects.get(is_template=True, owner=plan.coach)
        assert resp.status_code == 302
        assert resp.url == library_url()
        assert copy.relationship is None
        assert copy.title == plan.title
        assert copy.goal == plan.goal
        assert copy.status == Plan.Status.ACTIVE
        assert copy.unit == Unit.POUNDS
        meso = copy.mesocycles.get()
        assert (meso.name, meso.order, meso.week_count) == ("Block A", 2, 4)
        week = meso.weeks.get()
        assert (
            week.index,
            week.phase,
            week.volume,
            week.intensity,
            week.is_deload,
        ) == (
            1,
            "Accum",
            72,
            81,
            True,
        )
        slot = meso.session_slots.get()
        assert (slot.day_number, slot.name, slot.bias, slot.order) == (
            3,
            "Lower",
            "Squat",
            4,
        )
        row = slot.exercise_slots.get()
        assert row.name == cell.exercise_slot.name
        assert row.tags == ["main", "barbell"]
        assert (row.tempo, row.rest, row.note) == ("301", "3m", "Belt optional")
        assert list(week.cells.order_by("line").values_list("line", "text")) == [
            (0, "4 x 6, 225"),
            (1, "RPE 8"),
        ]

    def test_excludes_athlete_authored_logs_and_delivery_state(self, client):
        plan, cell = self._live_plan()
        athlete_line = sub_line(cell, "Athlete note 315 x 4", athlete_authored=True)
        week = cell.week
        week.delivered_at = timezone.now()
        week.save(update_fields=["delivered_at"])
        session = week.sessions.get()
        log = SessionLogFactory(session=session, athlete=plan.athlete)
        LoggedSetFactory(session_log=log, prescription=cell, source_line=athlete_line)
        WeekDeliveryFactory(week=week)
        client.force_login(plan.coach)

        client.post(save_as_template_url(plan))

        copy = Plan.objects.get(is_template=True, owner=plan.coach)
        assert not Prescription.objects.filter(
            exercise_slot__session_slot__mesocycle__plan=copy,
            text="Athlete note 315 x 4",
        ).exists()
        assert not LoggedSet.objects.filter(
            exercise_slot__session_slot__mesocycle__plan=copy
        ).exists()
        assert not SessionLog.objects.filter(
            session__week__mesocycle__plan=copy
        ).exists()
        assert not WeekDelivery.objects.filter(week__mesocycle__plan=copy).exists()
        assert copy.mesocycles.get().weeks.get().delivered_at is None

    def test_does_not_modify_source_plan(self, client):
        plan, cell = self._live_plan()
        client.force_login(plan.coach)

        client.post(save_as_template_url(plan))

        plan.refresh_from_db()
        assert plan.is_template is False
        assert plan.relationship_id is not None
        assert plan.status == Plan.Status.DRAFT
        assert Prescription.objects.filter(pk=cell.pk, text="4 x 6, 225").exists()

    def test_foreign_plan_404s(self, client):
        plan, _ = self._live_plan()
        client.force_login(CoachAthleteFactory().coach)

        resp = client.post(save_as_template_url(plan))

        assert resp.status_code == 404
        assert Plan.objects.filter(is_template=True).count() == 0

    def test_template_source_404s(self, client):
        tpl, _ = template_plan(title="Already template")
        client.force_login(tpl.owner)

        resp = client.post(save_as_template_url(tpl))

        assert resp.status_code == 404

    def test_get_is_not_allowed(self, client):
        plan, _ = self._live_plan()
        client.force_login(plan.coach)

        assert client.get(save_as_template_url(plan)).status_code == 405


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

    def test_does_not_copy_athlete_authored_lines(self, client):
        coach, rel = coach_with_client()
        tpl, cell = template_plan(coach, title="Base Block")
        sub_line(cell, "Coach cue", line=1)
        sub_line(cell, "Athlete typed this", line=2, athlete_authored=True)
        client.force_login(coach)

        client.post(use_url(tpl), {"relationship": rel.pk})

        copy = rel.plans.get()
        texts = set(
            Prescription.objects.filter(
                exercise_slot__session_slot__mesocycle__plan=copy
            ).values_list("text", flat=True)
        )
        assert "Coach cue" in texts
        assert "Athlete typed this" not in texts

    def test_matching_unit_starts_without_warning(self, client):
        coach, rel = coach_with_client()
        CoachProfileFactory(user=coach, default_unit=Unit.KILOGRAMS)
        tpl, _ = template_plan(coach, title="Base Block")
        tpl.unit = Unit.KILOGRAMS
        tpl.save(update_fields=["unit"])
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": rel.pk})

        assert resp.status_code == 302
        assert rel.plans.count() == 1

    def test_unit_mismatch_renders_confirmation_without_copying(self, client):
        coach, rel = coach_with_client()
        CoachProfileFactory(user=coach, default_unit=Unit.KILOGRAMS)
        tpl, _ = template_plan(coach, title="Base Block")
        tpl.unit = Unit.POUNDS
        tpl.save(update_fields=["unit"])
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": rel.pk})

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "lb" in body
        assert "kg" in body
        link_name = rel.athlete.display_name()
        assert link_name in body
        assert rel.plans.count() == 0

    def test_unit_mismatch_confirmed_copies_template_unit_and_text_verbatim(
        self, client
    ):
        coach, rel = coach_with_client()
        CoachProfileFactory(user=coach, default_unit=Unit.KILOGRAMS)
        tpl, cell = template_plan(coach, title="Base Block")
        tpl.unit = Unit.POUNDS
        tpl.save(update_fields=["unit"])
        cell.text = "4 x 6, 225"
        cell.save(update_fields=["text"])
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": rel.pk, "confirm_unit": "1"})

        copy = rel.plans.get()
        assert resp.status_code == 302
        assert copy.unit == Unit.POUNDS
        assert copy.mesocycles.get().weeks.get().cells.get(line=0).text == "4 x 6, 225"


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


class TestTemplateUseSuspension:
    """Finding 1 — ``template_use`` must honour the D6 soft-suspension freeze.

    A soft-suspended (over-seat-limit) relationship is never offered in the UI,
    so its presence in a POST is a stale/forged form; it must behave exactly like
    a foreign/invalid pick (flash + redirect to the library, nothing created) and
    must NOT start a live ACTIVE plan for a frozen client.
    """

    def test_suspended_relationship_creates_nothing(self, client):
        coach, _kept, suspended = _over_limit_coach()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": suspended.pk})

        assert resp.status_code == 302
        assert resp.url == library_url()
        assert suspended.plans.count() == 0
        assert Plan.objects.filter(is_template=False).count() == 0

    def test_kept_relationship_still_works_for_over_limit_coach(self, client):
        # Guard: the over-limit coach keeps starting templates for their oldest
        # (non-suspended) client — template_use gates on the target, not coarsely.
        coach, kept, _suspended = _over_limit_coach()
        tpl, _ = template_plan(coach, title="Base Block")
        client.force_login(coach)

        resp = client.post(use_url(tpl), {"relationship": kept.pk})

        copy = kept.plans.get()
        assert copy.is_template is False
        assert copy.status == Plan.Status.ACTIVE
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": copy.pk})


class TestBatchDeliverFromTemplateSuspension:
    """Finding 2 — batch-deliver from a TEMPLATE is per-target, not coarse-frozen.

    A template plan has no relationship, so the old ``can_edit_plan`` fell back to
    the coach-wide freeze and 402'd an over-limit coach entirely. D6 gates at the
    copy targets instead: an over-limit coach delivers from a template to their
    kept clients while suspended targets are dropped.
    """

    def test_over_limit_coach_delivers_only_to_kept_client(
        self, client, django_capture_on_commit_callbacks
    ):
        coach, kept, suspended = _over_limit_coach()
        tpl, _ = template_plan(coach, title="Squat Base")
        client.force_login(coach)

        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(
                batch_deliver_url(tpl),
                {"relationships": [kept.pk, suspended.pk]},
            )

        # Exactly one copy — for the kept client only; the suspended target is dropped.
        assert kept.plans.count() == 1
        assert suspended.plans.count() == 0
        assert Plan.objects.filter(is_template=False).count() == 1
        assert kept.plans.get().status == Plan.Status.ACTIVE
        assert resp.status_code == 302
        assert resp.url == library_url()

    def test_can_edit_plan_true_for_template_while_coach_frozen(self):
        # Unit: a template plan is never billing-frozen, even for an over-limit
        # coach whose coarse ``can_edit`` is False (templates aren't seats).
        coach, _kept, _suspended = _over_limit_coach()
        tpl, _ = template_plan(coach, title="Base Block")
        assert access.can_edit(coach) is False
        assert access.can_edit_plan(tpl) is True


class TestTemplateRosterDiscoverability:
    def test_roster_shows_start_from_template_only_with_templates_and_no_plan(
        self, client
    ):
        coach, rel = coach_with_client()
        template_plan(coach, title="Base Block")
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Start from a template" in body
        assert f"{library_url()}?athlete={rel.pk}" in body

    def test_roster_hides_start_from_template_without_templates(self, client):
        coach, _ = coach_with_client()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Start from a template" not in body

    def test_roster_hides_start_from_template_when_athlete_has_plan(self, client):
        coach, rel = coach_with_client()
        template_plan(coach, title="Base Block")
        PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Start from a template" not in body

    def test_pending_invite_copy_mentions_template(self, client):
        invite = CoachInviteFactory()
        client.force_login(invite.coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "hasn't accepted your invite yet" in body
        assert "template" in body
        assert "Write it as a template" in body
        assert library_url() in body


class TestDesignerTemplateStart:
    """#637 — a template's designer carries what "Start for a client…" needs.

    The island swaps Deliver (which a template refuses) for a client picker that
    posts to ``template_use``; the server hands it the action URL and the
    coach's deliverable clients through ``meso-designer-flags``.
    """

    @staticmethod
    def _flags(client, plan):
        import json
        import re

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        match = re.search(
            r'<script id="meso-designer-flags"[^>]*>(.*?)</script>',
            resp.content.decode(),
            re.S,
        )
        return json.loads(match.group(1))

    def test_template_flags_offer_start_for_client(self, client):
        coach, rel = coach_with_client()
        plan, _ = template_plan(owner=coach)
        client.force_login(coach)
        flags = self._flags(client, plan)
        assert flags["is_template"] is True
        assert flags["template_start"]["action"] == use_url(plan)
        assert [c["id"] for c in flags["template_start"]["clients"]] == [rel.pk]

    def test_pending_invites_lists_names_of_unexpired_invites_only(self, client):
        coach = CoachProfileFactory().user
        plan, _ = template_plan(owner=coach)
        CoachInviteFactory(
            coach=coach, email="jordan@example.com", label="Jordan Ellis"
        )
        CoachInviteFactory(
            coach=coach, email="sam@example.com"
        )  # no label: email local part
        CoachInviteFactory(
            coach=coach,
            email="old@example.com",
            label="Stale Pat",
            expires_at=timezone.now() - timedelta(days=1),
        )
        CoachInviteFactory(email="other-coach@example.com", label="Not Mine")
        client.force_login(coach)
        names = self._flags(client, plan)["template_start"]["pending_invites"]
        assert sorted(names) == ["Jordan Ellis", "sam"]

    def test_pending_invites_is_empty_without_invites(self, client):
        coach, _rel = coach_with_client()
        plan, _ = template_plan(owner=coach)
        client.force_login(coach)
        assert self._flags(client, plan)["template_start"]["pending_invites"] == []

    def test_suspended_client_is_not_offered(self, client):
        coach, _kept, suspended = _over_limit_coach()
        plan, _ = template_plan(owner=coach)
        client.force_login(coach)
        ids = [c["id"] for c in self._flags(client, plan)["template_start"]["clients"]]
        assert suspended.pk not in ids

    def test_client_plan_has_no_template_start(self, client):
        coach, rel = coach_with_client()
        plan = PlanFactory(relationship=rel)
        MesocycleFactory(plan=plan, order=0)
        client.force_login(coach)
        assert self._flags(client, plan)["template_start"] is None

    def test_start_lands_in_the_clients_copy(self, client):
        coach, rel = coach_with_client()
        plan, _ = template_plan(owner=coach)
        client.force_login(coach)
        resp = client.post(use_url(plan), {"relationship": rel.pk})
        copy = Plan.objects.get(relationship=rel, is_template=False)
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": copy.pk})


class TestDesignerInlineLinkColour:
    """#686 — ``.meso-inline-link`` rendered white on the white popover.

    ``--accent-ink`` is the text-on-accent token (#fff); a link on the light
    designer surface needs ``--accent-deep``. The designer has no dark mode (its
    tokens are fixed inline in ``designer.html``), so one declaration is enough.
    jsdom can't compute the cascade, hence the CSS-content guard.
    """

    def test_inline_link_uses_the_accent_ink_not_the_on_accent_token(self):
        import re
        from pathlib import Path

        css = (
            Path(__file__).resolve().parents[4]
            / "frontend/designer/src/styles/designer-chat.css"
        ).read_text()
        match = re.search(r"^\.meso-inline-link\s*\{([^}]*)\}", css, re.M)
        assert match, ".meso-inline-link rule missing"
        body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
        assert "color: var(--accent-deep)" in body
        assert "--accent-ink" not in body
        assert "underline" in body
