"""2c — batch-deliver: independent copies replace the group fan-out (D1).

The group subsystem (one shared program + per-member overrides + live-linked
materialized snapshots) is gone; its replacement is ``Plan.duplicate_for`` — a
deep copy of the live program tree — fanned out by ``plan_batch_deliver`` to
the clients the coach picks on the deliver screen. Each recipient's copy is
fully independent and live-editable from that moment on.

Covered here:

- ``Plan.duplicate_for`` copies the whole *live* tree (blocks, days, rows —
  tempo/rest/note/tags included — weeks, sessions, and every cell's line
  stack), resets ``delivered_at``, and skips soft-deleted rows;
- the copy is independent: edits on either side never touch the other;
- ``POST plan/<id>/batch-deliver/`` creates + delivers one ACTIVE copy per
  picked client (weeks stamped, ``WeekDelivery`` snapshots written, one
  block-level email each), leaving the source plan's weeks unstamped;
- scoping: non-owner coaches 403; foreign / own-athlete / unknown picks are
  dropped; an empty or all-invalid selection delivers nothing;
- the deliver screen offers the coach's *other* active athletes as batch
  candidates.
"""

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import WeekDelivery
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as presc_
from ._helpers import sub_line

pytestmark = pytest.mark.django_db


def comp(coach):
    """A comped subscription so a multi-athlete coach isn't seat-suspended."""
    CoachSubscription.objects.create(
        coach=coach, status=CoachSubscription.Status.COMPED
    )
    return coach


def seed_source(coach=None):
    """A two-week plan with a sub-line, a skip, row columns, and dead rows.

    Deliberately includes everything ``duplicate_for`` must carry (sub-line,
    skipped cell, tempo/rest/note, tags) and everything it must NOT (a
    soft-deleted week, a soft-deleted exercise row).
    """
    rel = CoachAthleteFactory(coach=coach or UserFactory(), athlete=UserFactory())
    plan = PlanFactory(relationship=rel, title="Base 1", status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Block 1", order=0)
    week1 = WeekFactory(mesocycle=meso, index=1, phase="Accum")
    session = day(week1, day_number=1, name="Lower")
    cell = presc_(
        session,
        name="C1) Box Squat",
        text="4 x 6",
        tempo="201",
        rest="2-3m",
        note="Max fatigue",
    )
    cell.exercise_slot.tags = ["squat"]
    cell.exercise_slot.save(update_fields=["tags"])
    sub_line(cell, "RPE 8")
    week2 = WeekFactory(mesocycle=meso, index=2)
    day(week2, session_slot=session.session_slot)
    Prescription.objects.create(
        exercise_slot=cell.exercise_slot, week=week2, text="", skipped=True
    )
    # Dead rows the copy must not carry.
    dead_week = WeekFactory(mesocycle=meso, index=3)
    dead_week.soft_delete()
    dead_row = presc_(session, name="Retired row", text="3 x 10")
    dead_row.exercise_slot.soft_delete()
    return plan, cell


class TestDuplicateFor:
    def test_copies_full_live_tree(self):
        plan, cell = seed_source()
        other = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())

        copy = plan.duplicate_for(other)

        assert copy.pk != plan.pk
        assert copy.relationship == other
        assert copy.title == "Base 1"
        assert copy.unit == plan.unit
        assert copy.status == Plan.Status.DRAFT  # caller opts into ACTIVE
        block = copy.mesocycles.get()
        assert (block.name, block.order) == ("Block 1", 0)
        weeks = list(block.weeks.order_by("index"))
        assert [w.index for w in weeks] == [1, 2]  # dead week 3 not copied
        assert weeks[0].phase == "Accum"
        assert all(w.delivered_at is None for w in weeks)
        row = block.session_slots.get().exercise_slots.get()  # dead row skipped
        assert row.name == "C1) Box Squat"
        assert (row.tempo, row.rest, row.note) == ("201", "2-3m", "Max fatigue")
        assert row.tags == ["squat"]
        wk1_lines = {
            c.line: c.text for c in row.cells.filter(week=weeks[0]).order_by("line")
        }
        assert wk1_lines == {0: "4 x 6", 1: "RPE 8"}
        wk2 = row.cells.get(week=weeks[1])
        assert wk2.skipped and wk2.text == ""
        # Each copied week has its Session instance for the day.
        assert weeks[0].sessions.count() == 1
        assert weeks[1].sessions.count() == 1

    def test_copy_is_independent(self):
        plan, cell = seed_source()
        other = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        copy = plan.duplicate_for(other)

        copied_cell = (
            Prescription.objects.filter(
                week__mesocycle__plan=copy, line=0, text="4 x 6"
            )
            .select_related("exercise_slot")
            .get()
        )
        copied_cell.text = "5 x 5"
        copied_cell.save(update_fields=["text"])
        copied_cell.exercise_slot.name = "C1) Front Squat"
        copied_cell.exercise_slot.save(update_fields=["name"])

        cell.refresh_from_db()
        assert cell.text == "4 x 6"
        assert cell.exercise_slot.name == "C1) Box Squat"

    def test_multi_block_plan_copies_every_block(self):
        plan, _ = seed_source()
        meso2 = MesocycleFactory(plan=plan, name="Block 2", order=1)
        week = WeekFactory(mesocycle=meso2, index=1)
        presc_(day(week, day_number=1, name="Upper"), name="Bench", text="3 x 8")
        other = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())

        copy = plan.duplicate_for(other)

        assert list(copy.mesocycles.values_list("name", "order")) == [
            ("Block 1", 0),
            ("Block 2", 1),
        ]


class TestBatchDeliverEndpoint:
    def url(self, plan):
        return reverse("meso:plan_batch_deliver", kwargs={"plan_id": plan.pk})

    def test_delivers_an_active_copy_per_picked_client(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        plan, _ = seed_source(coach=comp(UserFactory()))
        rel_b = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        rel_c = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        client.force_login(plan.coach)

        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self.url(plan), {"relationships": [rel_b.pk, rel_c.pk]})

        assert resp.status_code == 302
        assert resp.url == reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        for rel in (rel_b, rel_c):
            copy = rel.plans.get()
            assert copy.status == Plan.Status.ACTIVE
            assert copy.title == "Base 1"
            copy_weeks = copy.mesocycles.get().weeks.filter(deleted_at__isnull=True)
            assert all(w.delivered_at is not None for w in copy_weeks)
            assert WeekDelivery.objects.filter(week__in=copy_weeks).count() == 2
        # The SOURCE plan is untouched — batch-deliver sends copies, not it.
        assert all(
            w.delivered_at is None
            for w in plan.mesocycles.get().weeks.filter(deleted_at__isnull=True)
        )
        # One block-level nudge per recipient.
        assert len(mailoutbox) == 2

    def test_foreign_own_and_unknown_picks_are_dropped(self, client):
        plan, _ = seed_source(coach=comp(UserFactory()))
        foreign = CoachAthleteFactory()  # someone else's athlete
        client.force_login(plan.coach)

        resp = client.post(
            self.url(plan),
            {"relationships": [foreign.pk, plan.relationship.pk, 999999]},
        )

        assert resp.status_code == 302
        assert foreign.plans.count() == 0
        assert plan.relationship.plans.count() == 1  # just the source

    def test_empty_selection_delivers_nothing(self, client):
        plan, _ = seed_source(coach=comp(UserFactory()))
        client.force_login(plan.coach)

        resp = client.post(self.url(plan), {})

        assert resp.status_code == 302
        assert Plan.objects.count() == 1

    def test_non_owner_403(self, client):
        plan, _ = seed_source(coach=comp(UserFactory()))
        rel_b = CoachAthleteFactory(coach=plan.coach, athlete=UserFactory())
        outsider = UserFactory()
        client.force_login(outsider)

        resp = client.post(self.url(plan), {"relationships": [rel_b.pk]})

        assert resp.status_code in (403, 404)
        assert rel_b.plans.count() == 0

    def test_deliver_screen_offers_other_clients(self, client):
        plan, _ = seed_source(coach=comp(UserFactory()))
        rel_b = CoachAthleteFactory(
            coach=plan.coach, athlete=UserFactory(name="Blake Doe")
        )
        client.force_login(plan.coach)

        resp = client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Blake Doe" in content
        assert f'value="{rel_b.pk}"' in content
