"""Groups slice — deliver the whole shared BLOCK to all members (P5).

Delivering a group's shared program **fans out** a per-athlete *resolved* copy of
the whole block: each active member gets every live week of the shared mesocycle
(the shared template + their override diffs) materialized into their own
individual plan, stamped delivered, so they see it through the unchanged athlete
surface (`/meso/me/` + the session logger) and get a single "your block is ready"
email/push — one per member, not one per week.

This is the group peer of the P3 individual whole-block delivery: both release
the whole mesocycle at once. A member's very first materialization mirrors the
source's ``is_current`` pointer (the week the athlete is on) rather than
flagging every delivered week current; every sync after that leaves it alone —
the athlete's own logging (issue #456) or the coach's manual override is what
moves it from there, so a re-delivery can never snap an advanced member back.

The modeling: a materialized plan is rooted at the member's `CoachAthlete`
relationship (an ordinary individual plan to the athlete surface) and tagged
with `source_group` so re-delivery refreshes the *same* plan and the coach's own
individual surfaces never see it.

These tests cover:

- `GroupMembership.sync_delivered_plan` — materializes one resolved plan per
  member, block-wide, idempotently, preserving logs while propagating edits;
- `MesoGroup.deliver_block` — the whole-block fan-out + its empty-state guards;
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
from store_project.meso.models import Week
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


def shared_meso(plan):
    """The shared program's (only) mesocycle — the block delivery targets."""
    return plan.mesocycles.get()


def append_shared_week(plan):
    """Grow the shared block to a second live week so a block has ≥2 to deliver.

    ``append_week`` adds a non-current, undelivered draft column — exactly what
    the multi-week designer does — so the block now holds week 1 (the scaffold's
    current week) plus week 2. Returns the new ``Week``.
    """
    return shared_meso(plan).append_week()


def shared_prescriptions(plan):
    """The shared program's current-week prescription cells, in (day, row) order."""
    week = shared_meso(plan).weeks.get(is_current=True)
    return list(
        Prescription.objects.filter(week=week)
        .select_related("exercise_slot", "exercise_slot__session_slot")
        .order_by("exercise_slot__session_slot__order", "exercise_slot__order")
    )


def live_weeks(plan):
    """The shared block's live weeks, in ``index`` order."""
    return list(
        shared_meso(plan).weeks.filter(deleted_at__isnull=True).order_by("index")
    )


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


# -- model: sync_delivered_plan (per-member block materialization) ------------


class TestSyncDeliveredPlan:
    def test_materializes_one_plan_per_member_rooted_at_relationship(self):
        group, plan, [m] = seed_group(member_count=1)
        meso = shared_meso(plan)

        member_plan, member_weeks = m.sync_delivered_plan(meso)

        assert member_plan.relationship_id == m.relationship_id
        assert member_plan.source_group_id == group.pk
        assert member_plan.group_id is None  # rooted at the relationship, not the group
        assert member_plan.athlete == m.relationship.athlete
        # The single scaffold week is materialized, mirroring its structure.
        assert [w.index for w in member_weeks] == [1]
        src_week = meso.weeks.get()
        assert member_weeks[0].sessions.count() == src_week.sessions.count()

    def test_materializes_every_live_week_of_the_block(self):
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)  # a second live week
        meso = shared_meso(plan)

        _, member_weeks = m.sync_delivered_plan(meso)

        assert [w.index for w in member_weeks] == [1, 2]
        src_weeks = meso.weeks.filter(deleted_at__isnull=True).order_by("index")
        for member_week, src_week in zip(member_weeks, src_weeks):
            assert member_week.sessions.count() == src_week.sessions.count()

    def test_member_week_is_current_mirrors_the_source(self):
        # Post-P3, ``is_current`` means "the week the athlete is on"; the member's
        # copy must mirror the source pointer (week 1), NOT flag every week current.
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)
        meso = shared_meso(plan)

        _, member_weeks = m.sync_delivered_plan(meso)

        by_index = {w.index: w for w in member_weeks}
        assert by_index[1].is_current is True
        assert by_index[2].is_current is False

    def test_member_week_is_current_follows_a_moved_source_pointer(self):
        # When the coach moves the shared current pointer to week 2, the member's
        # copy follows on the next sync.
        group, plan, [m] = seed_group(member_count=1)
        week2 = append_shared_week(plan)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)
        week1.is_current = False
        week1.save(update_fields=["is_current"])
        week2.is_current = True
        week2.save(update_fields=["is_current"])

        _, member_weeks = m.sync_delivered_plan(meso)

        by_index = {w.index: w for w in member_weeks}
        assert by_index[1].is_current is False
        assert by_index[2].is_current is True

    def test_resolves_override_for_the_member(self):
        group, plan, [m] = seed_group(member_count=1)
        first = shared_prescriptions(plan)[0]
        first.text = "3 x 10, RPE 7, 100"
        first.save(update_fields=["text"])
        m.set_override(first, load_pct=90, swap_name="Box Squat")

        _, member_weeks = m.sync_delivered_plan(shared_meso(plan))

        member_week = next(w for w in member_weeks if w.index == first.week.index)
        materialized = (
            member_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        # Text-first: the shared text is mirrored verbatim; the swap and the
        # load adjust land as extra freeform sub-lines, not field rewrites.
        assert materialized.text == "3 x 10, RPE 7, 100"
        sub_lines = list(
            Prescription.objects.filter(
                exercise_slot=materialized.exercise_slot,
                week=member_week,
                line__gte=1,
            )
            .order_by("line")
            .values_list("text", flat=True)
        )
        assert sub_lines == ["Box Squat", "90% of prescribed load"]

    def test_override_resolves_per_week(self):
        # An override targets one week's *cell*; only that week's materialized row
        # carries the diff, the other weeks stay on the shared base.
        group, plan, [m] = seed_group(member_count=1)
        week2 = append_shared_week(plan)
        meso = shared_meso(plan)
        # The same exercise slot in week 1 (current) and week 2.
        wk1_cell = shared_prescriptions(plan)[0]
        wk1_cell.text = "3 x 10, RPE 7, 100"
        wk1_cell.save(update_fields=["text"])
        wk2_cell = Prescription.objects.get(
            exercise_slot=wk1_cell.exercise_slot, week=week2, line=0
        )
        wk2_cell.text = "3 x 10, RPE 7, 100"
        wk2_cell.save(update_fields=["text"])
        # Override only week 2's cell.
        m.set_override(wk2_cell, load_pct=50)

        _, member_weeks = m.sync_delivered_plan(meso)

        by_index = {w.index: w for w in member_weeks}

        def sub_lines_for(week):
            cell = (
                week.sessions.get(
                    session_slot__day_number=wk1_cell.exercise_slot.session_slot.day_number
                )
                .cells()
                .get(exercise_slot__order=wk1_cell.exercise_slot.order)
            )
            return list(
                Prescription.objects.filter(
                    exercise_slot=cell.exercise_slot, week=week, line__gte=1
                )
                .exclude(text="")
                .order_by("line")
                .values_list("text", flat=True)
            )

        assert sub_lines_for(by_index[1]) == []  # untouched week
        assert sub_lines_for(by_index[2]) == ["50% of prescribed load"]

    def test_unadjusted_member_gets_the_shared_base(self):
        group, plan, [adjusted, plain] = seed_group(member_count=2)
        first = shared_prescriptions(plan)[0]
        first.text = "3 x 10, RPE 7, 100"
        first.save(update_fields=["text"])
        adjusted.set_override(first, load_pct=80)

        _, member_weeks = plain.sync_delivered_plan(shared_meso(plan))

        plain_week = next(w for w in member_weeks if w.index == first.week.index)
        row = (
            plain_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        assert row.name == first.name
        assert row.text == "3 x 10, RPE 7, 100"
        assert (
            not Prescription.objects.filter(
                exercise_slot=row.exercise_slot, week=plain_week, line__gte=1
            )
            .exclude(text="")
            .exists()
        )

    def test_skipped_shared_cell_stays_skipped_for_the_member(self):
        # A week the coach skipped for the shared lineup must not resurface in an
        # athlete's delivered plan (one-week exceptions + groups).
        group, plan, [m] = seed_group(member_count=1)
        first = shared_prescriptions(plan)[0]
        first.skipped = True
        first.save(update_fields=["skipped"])

        _, member_weeks = m.sync_delivered_plan(shared_meso(plan))

        member_week = next(w for w in member_weeks if w.index == first.week.index)
        materialized = (
            member_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        assert materialized.skipped is True

    def test_redelivery_reuses_the_same_plan(self):
        group, plan, [m] = seed_group(member_count=1)
        meso = shared_meso(plan)

        first_plan, _ = m.sync_delivered_plan(meso)
        second_plan, _ = m.sync_delivered_plan(meso)

        assert first_plan.pk == second_plan.pk
        assert (
            Plan.objects.filter(relationship=m.relationship, source_group=group).count()
            == 1
        )

    def test_redelivery_preserves_an_athletes_log(self):
        group, plan, [m] = seed_group(member_count=1)
        meso = shared_meso(plan)
        _, member_weeks = m.sync_delivered_plan(meso)
        session = member_weeks[0].sessions.first()
        log = SessionLogFactory(
            session=session, athlete=m.relationship.athlete, status="done"
        )

        # The coach re-delivers (no structural change) — the session row survives,
        # so the athlete's log isn't cascade-deleted.
        m.sync_delivered_plan(meso)

        assert SessionLog.objects.filter(pk=log.pk).exists()

    def test_redelivery_propagates_an_override_change(self):
        group, plan, [m] = seed_group(member_count=1)
        first = shared_prescriptions(plan)[0]
        first.text = "3 x 10, RPE 7, 100"
        first.save(update_fields=["text"])
        meso = shared_meso(plan)

        m.sync_delivered_plan(meso)
        m.set_override(first, load_pct=50)
        _, member_weeks = m.sync_delivered_plan(meso)

        member_week = next(w for w in member_weeks if w.index == first.week.index)
        row = (
            member_week.sessions.get(
                session_slot__day_number=first.exercise_slot.session_slot.day_number
            )
            .cells()
            .get(exercise_slot__order=first.exercise_slot.order)
        )
        sub_lines = list(
            Prescription.objects.filter(
                exercise_slot=row.exercise_slot, week=member_week, line__gte=1
            )
            .exclude(text="")
            .order_by("line")
            .values_list("text", flat=True)
        )
        assert sub_lines == ["50% of prescribed load"]

    def test_dropped_shared_prescription_hides_on_member_week(self):
        group, plan, [m] = seed_group(member_count=1)
        meso = shared_meso(plan)
        m.sync_delivered_plan(meso)
        # Drop a row from the shared program, then re-deliver.
        first = shared_prescriptions(plan)[0]
        day_number = first.exercise_slot.session_slot.day_number
        order = first.exercise_slot.order
        first.exercise_slot.soft_delete()

        _, member_weeks = m.sync_delivered_plan(meso)

        member_week = member_weeks[0]
        member_slot = member_week.mesocycle.session_slots.get(day_number=day_number)
        # The member's copy is *hidden*, never hard-deleted (soft delete,
        # designer framework Phase 0): the member's LoggedSets may reference
        # it, and a source row that returns revives it in place.
        dropped = member_slot.exercise_slots.get(order=order)
        assert dropped.deleted_at is not None
        src_week = meso.weeks.get()
        live_src_day = src_week.sessions.get(session_slot__day_number=day_number)
        assert (
            member_slot.exercise_slots.filter(deleted_at__isnull=True).count()
            == live_src_day.cells().count()
        )

    def test_dropped_shared_week_hides_on_member_side(self):
        # A whole week removed from the shared block soft-deletes the member's
        # matching week (never hard-deletes — logged history stays recoverable).
        group, plan, [m] = seed_group(member_count=1)
        week2 = append_shared_week(plan)
        meso = shared_meso(plan)
        member_plan, _ = m.sync_delivered_plan(meso)
        member_meso = member_plan.mesocycles.get()
        assert member_meso.weeks.filter(deleted_at__isnull=True).count() == 2

        week2.soft_delete()
        m.sync_delivered_plan(meso)

        dropped = member_meso.weeks.get(index=2)
        assert dropped.deleted_at is not None
        assert member_meso.weeks.filter(deleted_at__isnull=True).count() == 1

    def test_member_advance_is_preserved_across_redelivery(self):
        # Issue #456: post-first-sync, a re-delivery must never snap an
        # athlete's own advanced pointer back to the coach's.
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)
        meso = shared_meso(plan)

        # First sync materializes + mirrors the source's current pointer (week 1).
        _, member_weeks = m.sync_delivered_plan(meso)
        by_index = {w.index: w for w in member_weeks}
        assert by_index[1].is_current is True
        assert by_index[2].is_current is False

        # The member advances to week 2 on their own (e.g. by logging it).
        by_index[1].is_current = False
        by_index[1].save(update_fields=["is_current"])
        by_index[2].is_current = True
        by_index[2].save(update_fields=["is_current"])

        # The coach re-delivers (e.g. tweaks a load) — must NOT snap them back.
        m.sync_delivered_plan(meso)

        by_index[1].refresh_from_db()
        by_index[2].refresh_from_db()
        assert by_index[2].is_current is True
        assert by_index[1].is_current is False

    def test_source_pointer_move_does_not_move_an_already_materialized_member(self):
        # Post-first-sync, a re-sync never touches ``is_current`` — even when
        # the coach moves the shared pointer.
        group, plan, [m] = seed_group(member_count=1)
        week2 = append_shared_week(plan)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)

        m.sync_delivered_plan(meso)  # first sync mirrors week 1

        week1.is_current = False
        week1.save(update_fields=["is_current"])
        week2.is_current = True
        week2.save(update_fields=["is_current"])

        _, member_weeks = m.sync_delivered_plan(meso)  # re-sync

        by_index = {w.index: w for w in member_weeks}
        assert by_index[1].is_current is True
        assert by_index[2].is_current is False

    def test_new_week_added_after_first_sync_materializes_not_current(self):
        # A week the member never had before must not materialize current just
        # because it happens to be the source's current pointer — otherwise a
        # member who already advanced past first sync would end up with two
        # ``is_current`` weeks (nothing in the DB prevents that).
        group, plan, [m] = seed_group(member_count=1)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)

        _, member_weeks = m.sync_delivered_plan(meso)  # first sync: week 1 only
        assert [w.index for w in member_weeks] == [1]

        week2 = append_shared_week(plan)
        week1.is_current = False
        week1.save(update_fields=["is_current"])
        week2.is_current = True
        week2.save(update_fields=["is_current"])

        _, member_weeks = m.sync_delivered_plan(meso)

        by_index = {w.index: w for w in member_weeks}
        assert by_index[2].is_current is False
        # The member's own already-materialized pointer stays untouched too.
        assert by_index[1].is_current is True

    def test_revived_week_drops_stale_current_when_member_has_moved_on(self):
        # Issue #456 nit 1: ``Week.soft_delete`` never clears ``is_current``, so
        # a week dropped from the block while it was the member's current one
        # keeps that stale ``True`` on its dead row. Real ``advance_current_week``
        # only clears LIVE weeks' pointer (``.filter(deleted_at__isnull=True)``),
        # so it never touches the dead week's stale flag either — it just
        # quietly survives. If the coach later brings the week back, reviving
        # it verbatim would resurrect a second live current week alongside
        # wherever the member has since moved on — the member's real position
        # must win instead.
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)
        append_shared_week(plan)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)

        m.sync_delivered_plan(meso)  # first sync mirrors week 1 as current
        member_plan = Plan.objects.get(relationship=m.relationship, source_group=group)
        member_meso = member_plan.mesocycles.get()
        assert member_meso.weeks.get(index=1).is_current is True

        # The coach drops week 1 from the shared block — the member's copy is
        # soft-deleted, but its stale ``is_current`` survives the drop.
        week1.soft_delete()
        m.sync_delivered_plan(meso)
        member_week1 = member_meso.weeks.get(index=1)
        assert member_week1.deleted_at is not None
        assert member_week1.is_current is True

        # The member's own logging moves their pointer to week 3 — mirroring
        # ``Week.advance_current_week``, which only clears live weeks, so it
        # never touches the dead week 1's stale flag.
        member_week3 = member_meso.weeks.get(index=3)
        member_week3.is_current = True
        member_week3.save(update_fields=["is_current"])

        # The coach re-adds week 1 and re-delivers.
        week1.deleted_at = None
        week1.save(update_fields=["deleted_at"])
        m.sync_delivered_plan(meso)

        member_week1.refresh_from_db()
        member_week3.refresh_from_db()
        assert member_week1.deleted_at is None  # revived
        assert member_week1.is_current is False  # stale flag dropped
        assert member_week3.is_current is True  # member's real position wins
        assert (
            Week.objects.filter(
                mesocycle__plan=member_plan, is_current=True, deleted_at__isnull=True
            ).count()
            == 1
        )

    def test_revived_week_keeps_stale_current_when_member_has_no_other_current(self):
        # Issue #456 nit 1, the flip side: when the member never moved past the
        # dropped week (so they have NO other live current week once it's
        # dead), reviving it should restore their position rather than
        # stranding them positionless.
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)

        m.sync_delivered_plan(meso)  # first sync: week 1 current
        member_plan = Plan.objects.get(relationship=m.relationship, source_group=group)
        member_meso = member_plan.mesocycles.get()
        assert member_meso.weeks.get(index=1).is_current is True

        week1.soft_delete()
        m.sync_delivered_plan(meso)
        member_week1 = member_meso.weeks.get(index=1)
        assert member_week1.deleted_at is not None
        assert member_week1.is_current is True  # stale flag survives the drop
        # The member now has zero LIVE current weeks — their only current week
        # is dead.
        assert not Week.objects.filter(
            mesocycle__plan=member_plan, is_current=True, deleted_at__isnull=True
        ).exists()

        week1.deleted_at = None
        week1.save(update_fields=["deleted_at"])
        m.sync_delivered_plan(meso)

        member_week1.refresh_from_db()
        assert member_week1.deleted_at is None  # revived
        assert member_week1.is_current is True  # position restored
        assert (
            Week.objects.filter(
                mesocycle__plan=member_plan, is_current=True, deleted_at__isnull=True
            ).count()
            == 1
        )

    def test_revival_ignores_a_current_week_the_same_sync_drops(self):
        # Issue #456 nit follow-up: ONE sync can both revive the member's old
        # stale-current week AND drop the week they had advanced to. The
        # revival's "does the member have another live current week?" check
        # must not count a week this very sync is about to soft-delete —
        # otherwise it clears the revived flag, then the drop loop kills the
        # counted week, and the member plan ends with ZERO live current weeks.
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)
        append_shared_week(plan)
        meso = shared_meso(plan)
        week1 = meso.weeks.get(index=1)
        week3 = meso.weeks.get(index=3)

        m.sync_delivered_plan(meso)  # first sync mirrors week 1 as current
        member_plan = Plan.objects.get(relationship=m.relationship, source_group=group)
        member_meso = member_plan.mesocycles.get()

        # Coach drops week 1; the member's dead copy keeps its stale flag.
        week1.soft_delete()
        m.sync_delivered_plan(meso)
        member_week1 = member_meso.weeks.get(index=1)
        assert member_week1.deleted_at is not None
        assert member_week1.is_current is True

        # The member advances to week 3 (their real position).
        member_week3 = member_meso.weeks.get(index=3)
        member_week3.is_current = True
        member_week3.save(update_fields=["is_current"])

        # In ONE shared-block edit the coach brings week 1 back and removes
        # week 3, then re-delivers.
        week1.deleted_at = None
        week1.save(update_fields=["deleted_at"])
        week3.soft_delete()
        m.sync_delivered_plan(meso)

        member_week1.refresh_from_db()
        member_week3.refresh_from_db()
        assert member_week1.deleted_at is None  # revived
        assert member_week3.deleted_at is not None  # dropped with its source
        # The doomed week 3 must not have counted as the member's position:
        # the revived week keeps the pointer, leaving exactly one live current.
        assert member_week1.is_current is True
        assert (
            Week.objects.filter(
                mesocycle__plan=member_plan, is_current=True, deleted_at__isnull=True
            ).count()
            == 1
        )


# -- model: deliver_block (the whole-block fan-out) --------------------------


class TestDeliverBlock:
    def test_stamps_every_member_week_and_every_shared_week(self):
        group, plan, memberships = seed_group(member_count=2)
        append_shared_week(plan)  # a two-week block
        shared_live = live_weeks(plan)
        assert len(shared_live) == 2

        now, delivered = group.deliver_block()

        assert len(delivered) == 2
        for member_plan, member_weeks in delivered:
            assert len(member_weeks) == 2
            for member_week in member_weeks:
                assert member_week.delivered_at == now
                assert WeekDelivery.objects.filter(week=member_week).count() == 1
        for shared_week in shared_live:
            shared_week.refresh_from_db()
            assert shared_week.delivered_at == now
            assert WeekDelivery.objects.filter(week=shared_week).count() == 1

    def test_member_weeks_mirror_the_source_current_pointer(self):
        group, plan, [m] = seed_group(member_count=1)
        append_shared_week(plan)

        group.deliver_block()

        member_meso = Plan.objects.get(source_group=group).mesocycles.get()
        assert member_meso.weeks.get(index=1).is_current is True
        assert member_meso.weeks.get(index=2).is_current is False

    def test_raises_without_a_shared_plan(self):
        group = MesoGroupFactory()
        with pytest.raises(InvalidTransition):
            group.deliver_block()

    def test_raises_without_members(self):
        group, _, _ = seed_group(member_count=0)
        with pytest.raises(InvalidTransition):
            group.deliver_block()

    def test_skips_a_member_whose_link_ended(self):
        group, plan, [stays, leaves] = seed_group(member_count=2)
        leaves.relationship.end()

        _, delivered = group.deliver_block()

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

        group.deliver_block(older)

        materialized = Plan.objects.get(source_group=group, relationship=m.relationship)
        assert materialized.title == older.title


# -- queryset tenancy + athlete-surface reuse --------------------------------


class TestMaterializedPlanScoping:
    def test_hidden_from_coach_surfaces_visible_to_athlete(self):
        group, plan, [m] = seed_group(member_count=1)
        coach = group.coach
        group.deliver_block()
        member_plan = Plan.objects.get(source_group=group)

        # The coach manages the group only through the shared program — the
        # derived snapshot never appears in their individual designer/deliver flows.
        assert member_plan not in Plan.objects.for_coach(coach)
        assert member_plan not in Plan.objects.editable_by(coach)
        # The athlete, however, sees exactly this.
        assert member_plan in Plan.objects.for_athlete(m.relationship.athlete)

    def test_member_sees_delivered_week_on_athlete_home(self, client):
        group, plan, [m] = seed_group(member_count=1)
        group.deliver_block()
        athlete = m.relationship.athlete
        client.force_login(athlete)

        body = client.get(reverse("meso:athlete_home")).content.decode()

        assert plan.title in body

    def test_member_can_open_a_delivered_session(self, client):
        group, plan, [m] = seed_group(member_count=1)
        group.deliver_block()
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
        group.deliver_block()
        member_plan = Plan.objects.get(source_group=group)
        assert member_plan.is_editable_by(group.coach) is False
        client.force_login(group.coach)

        resp = client.post(deliver_url(member_plan))

        assert resp.status_code == 403


class TestRemovedMemberLosesAccess:
    def test_remove_athlete_archives_their_materialized_plan(self):
        group, plan, [stays, leaves] = seed_group(member_count=2)
        group.deliver_block()
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
        assert data["week_count"] == 1
        assert Plan.objects.filter(source_group=group).count() == 2

    def test_group_plan_fans_out_the_whole_block(self, client):
        group, plan, memberships = seed_group(member_count=2)
        append_shared_week(plan)  # a two-week block
        client.force_login(group.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        assert resp.json()["week_count"] == 2
        # Each member's materialized plan carries both delivered weeks.
        for m in memberships:
            member_plan = Plan.objects.get(
                source_group=group, relationship=m.relationship
            )
            assert (
                member_plan.mesocycles.get()
                .weeks.filter(delivered_at__isnull=False)
                .count()
                == 2
            )

    def test_group_ignores_a_week_id_and_fans_out_the_block(self, client):
        # Per-week delivery is an individual-designer affordance; a group plan
        # always fans out its whole block, so a stray ``week_id`` in the body is
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

    def test_notifies_each_member_once_for_the_whole_block(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        # A two-week block delivers ONE nudge per member (the block notifier),
        # never one email per week — so member_count emails, not member_count×weeks.
        coach = UserFactory(name="Coach Lance", email="coach@example.com")
        group, plan, memberships = seed_group(coach=coach, member_count=2)
        append_shared_week(plan)  # two live weeks
        emails = {m.relationship.athlete.email for m in memberships}
        client.force_login(coach)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(deliver_url(plan))

        assert len(mailoutbox) == 2  # one per member, NOT one per member per week
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

    def test_flash_names_the_block_and_week_count(self, client):
        group, plan, memberships = seed_group(member_count=2)
        append_shared_week(plan)  # two live weeks
        client.force_login(group.coach)

        resp = client.post(group_deliver_url(group), follow=True)

        body = resp.content.decode()
        assert "Delivered the block (2 weeks) to 2 members." in body

    def test_flash_singular_block_and_member(self, client):
        group, plan, [m] = seed_group(member_count=1)
        client.force_login(group.coach)

        resp = client.post(group_deliver_url(group), follow=True)

        body = resp.content.decode()
        assert "Delivered the block (1 week) to 1 member." in body

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
        # The button names the whole block, not a single week (P5).
        assert "Deliver this block" in body

    def test_detail_page_hides_deliver_button_without_members(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        group.create_shared_plan()
        client.force_login(coach)
        body = client.get(
            reverse("meso:group", kwargs={"pk": group.pk})
        ).content.decode()
        assert group_deliver_url(group) not in body
