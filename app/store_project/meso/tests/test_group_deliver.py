"""Groups slice (S1) Phase 4 — deliver to all members.

Delivering a group's shared week **fans out** a per-athlete *resolved* program:
each active member gets their effective week (the shared template + their
override diffs) materialized into their own individual plan, stamped delivered,
so they see it through the unchanged athlete surface (`/meso/me/` + the session
logger) and get the same delivery email/push the individual slice built.

The modeling: a materialized plan is rooted at the member's `CoachAthlete`
relationship (an ordinary individual plan to the athlete surface) and tagged
with `source_group` so re-delivery refreshes the *same* plan and the coach's own
individual surfaces never see it.

These tests cover:

- `GroupMembership.sync_delivered_plan` — materializes one resolved plan per
  member, idempotently, preserving logs while propagating edits;
- `MesoGroup.deliver_current_week` — the fan-out + its empty-state guards;
- the queryset tenancy (`for_coach`/`editable_by` hide it, `for_athlete` shows it)
  and the athlete-surface reuse;
- `plan_deliver` (the group-aware JSON endpoint) + its notifications + guards;
- `group_deliver` (the coach-facing form POST) + the group-detail button.
"""

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import InvalidTransition
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import WeekDelivery
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_group(coach=None, *, member_count=2, focus="Strength"):
    """A coach-owned group with a shared program (current week) + N members.

    The shared program is the two-day starter scaffold ``create_shared_plan``
    builds; ``member_count`` athletes are added off fresh active links. Returns
    ``(group, shared_plan, [membership, ...])``.
    """
    coach = coach or UserFactory()
    # A coach with a multi-member group is a paying coach (S6 Phase 3) —
    # comp so the D6 over-limit edit/deliver freeze doesn't fire here.
    CoachSubscription.comp(coach)
    group = MesoGroupFactory(coach=coach, focus=focus)
    memberships = []
    for _ in range(member_count):
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        memberships.append(group.add_athlete(athlete))
    plan = group.create_shared_plan()
    return group, plan, memberships


def shared_prescriptions(plan):
    """The shared program's current-week prescription cells, in (day, row) order."""
    week = plan.mesocycles.get().weeks.get()
    return list(
        Prescription.objects.filter(week=week)
        .select_related("exercise_slot", "exercise_slot__session_slot")
        .order_by("exercise_slot__session_slot__order", "exercise_slot__order")
    )


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


# -- model: sync_delivered_plan (per-member materialization) ------------------


class TestSyncDeliveredPlan:
    def test_materializes_one_plan_per_member_rooted_at_relationship(self):
        group, plan, [m] = seed_group(member_count=1)
        group_week = plan.mesocycles.get().weeks.get()

        member_plan, member_week = m.sync_delivered_plan(group_week)

        assert member_plan.relationship_id == m.relationship_id
        assert member_plan.source_group_id == group.pk
        assert member_plan.group_id is None  # rooted at the relationship, not the group
        assert member_plan.athlete == m.relationship.athlete
        # The materialized week mirrors the shared week's structure.
        assert member_week.index == group_week.index
        assert member_week.sessions.count() == group_week.sessions.count()

    def test_resolves_override_for_the_member(self):
        group, plan, [m] = seed_group(member_count=1)
        first = shared_prescriptions(plan)[0]
        first.load = "100"
        first.save(update_fields=["load"])
        m.set_override(first, load_pct=90, swap_name="Box Squat")

        _, member_week = m.sync_delivered_plan(first.week)

        materialized = (
            member_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        assert materialized.name == "Box Squat"
        assert materialized.load == "90"  # 90% of 100, round-to-2.5

    def test_unadjusted_member_gets_the_shared_base(self):
        group, plan, [adjusted, plain] = seed_group(member_count=2)
        first = shared_prescriptions(plan)[0]
        first.load = "100"
        first.save(update_fields=["load"])
        adjusted.set_override(first, load_pct=80)

        _, plain_week = plain.sync_delivered_plan(first.week)

        row = (
            plain_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        assert row.name == first.name
        assert row.load == "100"

    def test_redelivery_reuses_the_same_plan(self):
        group, plan, [m] = seed_group(member_count=1)
        group_week = plan.mesocycles.get().weeks.get()

        first_plan, _ = m.sync_delivered_plan(group_week)
        second_plan, _ = m.sync_delivered_plan(group_week)

        assert first_plan.pk == second_plan.pk
        assert (
            Plan.objects.filter(relationship=m.relationship, source_group=group).count()
            == 1
        )

    def test_redelivery_preserves_an_athletes_log(self):
        group, plan, [m] = seed_group(member_count=1)
        group_week = plan.mesocycles.get().weeks.get()
        _, member_week = m.sync_delivered_plan(group_week)
        session = member_week.sessions.first()
        log = SessionLogFactory(
            session=session, athlete=m.relationship.athlete, status="done"
        )

        # The coach re-delivers (no structural change) — the session row survives,
        # so the athlete's log isn't cascade-deleted.
        m.sync_delivered_plan(group_week)

        assert SessionLog.objects.filter(pk=log.pk).exists()

    def test_redelivery_propagates_an_override_change(self):
        group, plan, [m] = seed_group(member_count=1)
        first = shared_prescriptions(plan)[0]
        first.load = "100"
        first.save(update_fields=["load"])
        group_week = first.week

        m.sync_delivered_plan(group_week)
        m.set_override(first, load_pct=50)
        _, member_week = m.sync_delivered_plan(group_week)

        row = (
            member_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        assert row.load == "50"

    def test_dropped_shared_prescription_hides_on_member_week(self):
        group, plan, [m] = seed_group(member_count=1)
        group_week = plan.mesocycles.get().weeks.get()
        m.sync_delivered_plan(group_week)
        # Drop a row from the shared program, then re-deliver.
        first = shared_prescriptions(plan)[0]
        day_number = first.exercise_slot.session_slot.day_number
        order = first.exercise_slot.order
        first.exercise_slot.soft_delete()

        _, member_week = m.sync_delivered_plan(group_week)

        member_slot = member_week.mesocycle.session_slots.get(day_number=day_number)
        # The member's copy is *hidden*, never hard-deleted (soft delete,
        # designer framework Phase 0): the member's LoggedSets may reference
        # it, and a source row that returns revives it in place.
        dropped = member_slot.exercise_slots.get(order=order)
        assert dropped.deleted_at is not None
        live_group_day = group_week.sessions.get(session_slot__day_number=day_number)
        assert (
            member_slot.exercise_slots.filter(deleted_at__isnull=True).count()
            == live_group_day.cells().count()
        )


# -- model: deliver_current_week (the fan-out) -------------------------------


class TestDeliverCurrentWeek:
    def test_stamps_every_member_week_and_the_group_week(self):
        group, plan, memberships = seed_group(member_count=2)
        group_week = plan.mesocycles.get().weeks.get()

        now, delivered = group.deliver_current_week()

        assert len(delivered) == 2
        for member_plan, member_week in delivered:
            assert member_week.delivered_at == now
            assert WeekDelivery.objects.filter(week=member_week).count() == 1
        group_week.refresh_from_db()
        assert group_week.delivered_at == now

    def test_raises_without_a_shared_plan(self):
        group = MesoGroupFactory()
        with pytest.raises(InvalidTransition):
            group.deliver_current_week()

    def test_raises_without_members(self):
        group, _, _ = seed_group(member_count=0)
        with pytest.raises(InvalidTransition):
            group.deliver_current_week()

    def test_skips_a_member_whose_link_ended(self):
        group, plan, [stays, leaves] = seed_group(member_count=2)
        leaves.relationship.end()

        _, delivered = group.deliver_current_week()

        athletes = {p.athlete for p, _ in delivered}
        assert stays.relationship.athlete in athletes
        assert leaves.relationship.athlete not in athletes

    def test_delivers_the_requested_plan_not_the_newest(self):
        # A group holding more than one program delivers the plan it was *asked*
        # to, not whichever ``shared_plan()`` (most-recently-modified) reselects.
        group, older, [m] = seed_group(member_count=1)
        older.title = "Older block"
        older.save()  # ``shared_plan()`` would now prefer this newer one
        newer = group.create_shared_plan()
        assert group.shared_plan().pk == newer.pk

        group.deliver_current_week(older)

        materialized = Plan.objects.get(source_group=group, relationship=m.relationship)
        assert materialized.title == older.title


# -- queryset tenancy + athlete-surface reuse --------------------------------


class TestMaterializedPlanScoping:
    def test_hidden_from_coach_surfaces_visible_to_athlete(self):
        group, plan, [m] = seed_group(member_count=1)
        coach = group.coach
        group.deliver_current_week()
        member_plan = Plan.objects.get(source_group=group)

        # The coach manages the group only through the shared program — the
        # derived snapshot never appears in their individual designer/deliver flows.
        assert member_plan not in Plan.objects.for_coach(coach)
        assert member_plan not in Plan.objects.editable_by(coach)
        # The athlete, however, sees exactly this.
        assert member_plan in Plan.objects.for_athlete(m.relationship.athlete)

    def test_member_sees_delivered_week_on_athlete_home(self, client):
        group, plan, [m] = seed_group(member_count=1)
        group.deliver_current_week()
        athlete = m.relationship.athlete
        client.force_login(athlete)

        body = client.get(reverse("meso:athlete_home")).content.decode()

        assert plan.title in body

    def test_member_can_open_a_delivered_session(self, client):
        group, plan, [m] = seed_group(member_count=1)
        group.deliver_current_week()
        member_plan = Plan.objects.get(source_group=group)
        session = Session.objects.get(
            week__mesocycle__plan=member_plan, session_slot__day_number=1
        )
        client.force_login(m.relationship.athlete)

        resp = client.get(reverse("meso:athlete_session", kwargs={"pk": session.pk}))

        assert resp.status_code == 200

    def test_coach_cannot_edit_or_redeliver_a_materialized_plan(self, client):
        # The single-plan write gate (``is_editable_by``) must also exclude a
        # materialized snapshot — a coach posting its id directly to deliver /
        # autosave must not be able to mutate the athlete-facing copy.
        group, plan, [m] = seed_group(member_count=1)
        group.deliver_current_week()
        member_plan = Plan.objects.get(source_group=group)
        assert member_plan.is_editable_by(group.coach) is False
        client.force_login(group.coach)

        resp = client.post(deliver_url(member_plan))

        assert resp.status_code == 403


class TestRemovedMemberLosesAccess:
    def test_remove_athlete_archives_their_materialized_plan(self):
        group, plan, [stays, leaves] = seed_group(member_count=2)
        group.deliver_current_week()
        gone = leaves.relationship.athlete
        leaves_plan = Plan.objects.get(source_group=group, relationship__athlete=gone)

        group.remove_athlete(gone)

        leaves_plan.refresh_from_db()
        assert leaves_plan.status == Plan.Status.ARCHIVED
        # The removed member no longer sees the group's delivered program on their
        # surface (``_athlete_plans`` excludes archived), while the remaining
        # member keeps theirs.
        visible = Plan.objects.for_athlete(gone).exclude(status=Plan.Status.ARCHIVED)
        assert leaves_plan not in visible
        kept = stays.relationship.athlete
        kept_visible = Plan.objects.for_athlete(kept).exclude(
            status=Plan.Status.ARCHIVED
        )
        assert kept_visible.filter(source_group=group).exists()


# -- endpoint: plan_deliver (group-aware) ------------------------------------


class TestPlanDeliverGroup:
    def test_group_plan_fans_out(self, client):
        group, plan, memberships = seed_group(member_count=2)
        client.force_login(group.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        data = resp.json()
        assert data["ok"] is True
        assert data["members"] == 2
        assert data["delivered_at"]
        assert Plan.objects.filter(source_group=group).count() == 2

    def test_group_ignores_a_week_id_and_fans_out_current(self, client):
        # Per-week delivery is an individual-designer affordance; a group plan
        # always fans out its current week, so a stray ``week_id`` in the body is
        # ignored (the group branch short-circuits before the body is read).
        import json

        group, plan, memberships = seed_group(member_count=2)
        client.force_login(group.coach)

        resp = client.post(
            deliver_url(plan),
            data=json.dumps({"week_id": 999999}),
            content_type="application/json",
        )

        assert resp.status_code == 201
        assert resp.json()["members"] == 2
        assert Plan.objects.filter(source_group=group).count() == 2

    def test_notifies_each_member_once(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(name="Coach Lance", email="coach@example.com")
        group, plan, memberships = seed_group(coach=coach, member_count=2)
        emails = {m.relationship.athlete.email for m in memberships}
        client.force_login(coach)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(deliver_url(plan))

        assert len(mailoutbox) == 2
        assert {m.to[0] for m in mailoutbox} == emails

    def test_group_without_members_400(self, client):
        group, plan, _ = seed_group(member_count=0)
        client.force_login(group.coach)
        resp = client.post(deliver_url(plan))
        assert resp.status_code == 400
        assert not Plan.objects.filter(source_group=group).exists()

    def test_group_without_week_400(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        group.add_athlete(athlete)
        # A bare group plan with no mesocycle/week.
        plan = Plan.objects.create(
            group=group, title="Empty", status=Plan.Status.ACTIVE
        )
        client.force_login(coach)
        resp = client.post(deliver_url(plan))
        assert resp.status_code == 400

    def test_foreign_coach_forbidden(self, client):
        group, plan, _ = seed_group(member_count=1)
        client.force_login(UserFactory())
        resp = client.post(deliver_url(plan))
        assert resp.status_code == 403
        assert not Plan.objects.filter(source_group=group).exists()


# -- endpoint: group_deliver (coach-facing form POST) ------------------------


def group_deliver_url(group):
    return reverse("meso:group_deliver", kwargs={"pk": group.pk})


class TestGroupDeliverView:
    def test_delivers_and_redirects_to_group_detail(self, client):
        group, plan, memberships = seed_group(member_count=2)
        client.force_login(group.coach)

        resp = client.post(group_deliver_url(group))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:group", kwargs={"pk": group.pk})
        assert Plan.objects.filter(source_group=group).count() == 2

    def test_requires_a_shared_program(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        client.force_login(coach)

        resp = client.post(group_deliver_url(group))

        assert resp.status_code == 302
        assert not Plan.objects.filter(source_group=group).exists()

    def test_foreign_group_404(self, client):
        group, plan, _ = seed_group(member_count=1)
        client.force_login(UserFactory())
        resp = client.post(group_deliver_url(group))
        assert resp.status_code == 404
        assert not Plan.objects.filter(source_group=group).exists()

    def test_get_not_allowed(self, client):
        group, plan, _ = seed_group(member_count=1)
        client.force_login(group.coach)
        resp = client.get(group_deliver_url(group))
        assert resp.status_code == 405

    def test_requires_login(self, client):
        group, plan, _ = seed_group(member_count=1)
        resp = client.post(group_deliver_url(group))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_detail_page_shows_deliver_button_with_members(self, client):
        group, plan, _ = seed_group(member_count=1)
        client.force_login(group.coach)
        body = client.get(
            reverse("meso:group", kwargs={"pk": group.pk})
        ).content.decode()
        assert group_deliver_url(group) in body

    def test_detail_page_hides_deliver_button_without_members(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        group.create_shared_plan()
        client.force_login(coach)
        body = client.get(
            reverse("meso:group", kwargs={"pk": group.pk})
        ).content.decode()
        assert group_deliver_url(group) not in body
