"""Changes-since-last-delivery diff (the deferred "full diff UI").

Delivering a week records a ``WeekDelivery`` snapshot (``serialize_week_snapshot``).
When a coach comes back to re-deliver, the deliver screen should show **what
changed since the last delivery** — a diff of the week's live grid against the
most recent delivered snapshot. The snapshot data was always captured; this is
the UI that finally reads it (persistence-plan open assumption #3).

Three seams, mirroring the slice discipline:

- ``diff_week_snapshots`` — the pure diff over two snapshot payloads (matched by
  stable pks): added / removed / changed exercise rows grouped by session, whole
  sessions added/removed, and week-meta changes;
- ``presenters.deliver_screen`` — surfaces ``deliver["changes"]`` (``None`` on a
  first delivery; the diff on a re-delivery);
- ``DeliverView`` — the screen renders the diff on a re-delivery.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import WeekDelivery
from store_project.meso.serializers import diff_week_snapshots
from store_project.meso.serializers import serialize_week_snapshot
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as presc_

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# Pure diff over two snapshot payloads (no DB)                                 #
# --------------------------------------------------------------------------- #


def _presc(pk, name, **over):
    row = {
        "id": pk,
        "name": name,
        "sets": "3",
        "reps": "10",
        "load": "60",
        "load_type": "abs",
        "rpe": "7",
        "note": "",
    }
    row.update(over)
    return row


def _session(pk, n, name, exercises, bias=""):
    return {"id": pk, "n": n, "name": name, "bias": bias, "exercises": exercises}


def _snap(sessions, **week_over):
    week = {
        "id": 1,
        "index": 1,
        "phase": "Accum",
        "volume": 70,
        "intensity": 65,
        "is_deload": False,
    }
    week.update(week_over)
    return {"week": week, "sessions": sessions}


class TestDiffWeekSnapshots:
    def test_no_prior_payload_returns_none(self):
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        assert diff_week_snapshots(cur, None) is None
        assert diff_week_snapshots(cur, {}) is None

    def test_identical_snapshots_have_no_changes(self):
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        diff = diff_week_snapshots(cur, prev)
        assert diff["has_changes"] is False
        assert diff["sessions"] == []
        assert diff["added_sessions"] == []
        assert diff["removed_sessions"] == []
        assert diff["week"] == []

    def test_changed_load_is_a_changed_row(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", load="100")])])
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", load="105")])])
        diff = diff_week_snapshots(cur, prev)
        assert diff["has_changes"] is True
        (sess,) = diff["sessions"]
        assert sess["name"] == "Lower"
        (changed,) = sess["changed"]
        assert changed["name"] == "Squat"
        (field,) = changed["fields"]
        assert field["field"] == "load"
        assert field["before"] == "100"
        assert field["after"] == "105"

    def test_swap_is_a_changed_name(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Back Squat")])])
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Front Squat")])])
        diff = diff_week_snapshots(cur, prev)
        (sess,) = diff["sessions"]
        (changed,) = sess["changed"]
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["name"] == ("Back Squat", "Front Squat")

    def test_added_row(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        cur = _snap(
            [
                _session(
                    1,
                    1,
                    "Lower",
                    [_presc(10, "Squat"), _presc(11, "RDL", sets="3", reps="8")],
                )
            ]
        )
        diff = diff_week_snapshots(cur, prev)
        (sess,) = diff["sessions"]
        assert sess["changed"] == []
        (added,) = sess["added"]
        assert "RDL" in added["label"]
        assert "3" in added["label"] and "8" in added["label"]

    def test_removed_row(self):
        prev = _snap(
            [_session(1, 1, "Lower", [_presc(10, "Squat"), _presc(11, "Leg Press")])]
        )
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        diff = diff_week_snapshots(cur, prev)
        (sess,) = diff["sessions"]
        (removed,) = sess["removed"]
        assert "Leg Press" in removed["label"]

    def test_added_session(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        cur = _snap(
            [
                _session(1, 1, "Lower", [_presc(10, "Squat")]),
                _session(2, 2, "Upper", [_presc(20, "Bench"), _presc(21, "Row")]),
            ]
        )
        diff = diff_week_snapshots(cur, prev)
        assert diff["has_changes"] is True
        (added,) = diff["added_sessions"]
        assert added["name"] == "Upper"
        assert added["count"] == 2
        # A wholly-new session isn't double-counted as a per-session row diff.
        assert diff["sessions"] == []

    def test_removed_session(self):
        prev = _snap(
            [
                _session(1, 1, "Lower", [_presc(10, "Squat")]),
                _session(2, 2, "Upper", [_presc(20, "Bench")]),
            ]
        )
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        diff = diff_week_snapshots(cur, prev)
        (removed,) = diff["removed_sessions"]
        assert removed["name"] == "Upper"

    def test_week_meta_change(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])], is_deload=False)
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])], is_deload=True)
        diff = diff_week_snapshots(cur, prev)
        assert diff["has_changes"] is True
        (field,) = diff["week"]
        assert field["field"] == "is_deload"
        assert field["before"] is False
        assert field["after"] is True


# --------------------------------------------------------------------------- #
# Presenter + view (DB-backed)                                                 #
# --------------------------------------------------------------------------- #


def seed_plan(coach=None, athlete=None, load="111"):
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    presc = presc_(session, name="Box Squat", sets="4", reps="6", load=load, rpe="7")
    return plan, week, session, presc


def record_delivery(week):
    """Snapshot ``week`` as a delivery, exactly as ``plan_deliver`` does."""
    now = timezone.now()
    week.delivered_at = now
    week.save(update_fields=["delivered_at"])
    return WeekDelivery.objects.create(
        week=week, delivered_at=now, payload=serialize_week_snapshot(week)
    )


class TestDeliverScreenChanges:
    def test_first_delivery_has_no_diff(self):
        plan, _, _, _ = seed_plan()
        deliver = presenters.deliver_screen(plan)["deliver"]
        assert deliver["is_redelivery"] is False
        assert deliver["changes"] is None

    def test_redelivery_after_edit_surfaces_the_change(self):
        plan, week, _, presc = seed_plan(load="111")
        record_delivery(week)
        presc.load = "222"
        presc.save(update_fields=["load"])

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        changes = deliver["changes"]
        assert changes is not None
        assert changes["has_changes"] is True
        (sess,) = changes["sessions"]
        (changed,) = sess["changed"]
        assert changed["name"] == "Box Squat"
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["load"] == ("111", "222")

    def test_redelivery_after_skip_surfaces_the_change(self):
        plan, week, _, presc = seed_plan()
        record_delivery(week)
        presc.skipped = True
        presc.save(update_fields=["skipped"])

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        changes = deliver["changes"]
        assert changes is not None
        assert changes["has_changes"] is True
        (sess,) = changes["sessions"]
        (changed,) = sess["changed"]
        assert changed["name"] == "Box Squat"
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["skipped"] == (False, True)

    def test_redelivery_with_no_edits_reports_no_changes(self):
        plan, week, _, _ = seed_plan()
        record_delivery(week)

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        assert deliver["changes"] is not None
        assert deliver["changes"]["has_changes"] is False

    def test_diff_targets_the_chosen_week(self):
        # A coach can deliver a built-ahead week; the diff must compare *that*
        # week's snapshot, not the live week's.
        plan, week1, _, _ = seed_plan()
        meso = plan.mesocycles.first()
        week2 = WeekFactory(mesocycle=meso, index=2, is_current=False)
        session2 = day(week2, day_number=2, name="Upper")
        presc2 = presc_(session2, name="Bench", sets="3", reps="5", load="80")
        record_delivery(week2)
        presc2.load = "90"
        presc2.save(update_fields=["load"])

        deliver = presenters.deliver_screen(plan, week=week2)["deliver"]

        assert deliver["week_id"] == week2.pk
        (sess,) = deliver["changes"]["sessions"]
        (changed,) = sess["changed"]
        assert changed["name"] == "Bench"


class TestDeliverScreenRendersDiff:
    def _screen(self, client, plan):
        return client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))

    def test_first_delivery_screen_says_first_delivery(self, client):
        plan, _, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        body = self._screen(client, plan).content.decode()
        assert "First delivery" in body

    def test_redelivery_screen_renders_the_diff(self, client):
        plan, week, _, presc = seed_plan(load="111")
        record_delivery(week)
        presc.load = "222"
        presc.save(update_fields=["load"])
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        # The changed exercise and its before/after load are both surfaced.
        assert "Box Squat" in body
        assert "111" in body
        assert "222" in body

    def test_redelivery_screen_with_no_edits_says_no_changes(self, client):
        plan, week, _, _ = seed_plan()
        record_delivery(week)
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        assert "No changes" in body

    def test_redelivery_screen_renders_a_zero_week_value(self, client):
        # 0 is a valid volume/intensity (the model default), so a change *to* 0
        # must render the number — not get swallowed by an em-dash placeholder.
        plan, week, _, _ = seed_plan()
        record_delivery(week)  # snapshot at the factory's volume=70
        week.volume = 0
        week.save(update_fields=["volume"])
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        assert "Volume" in body
        # The new value 0 is rendered, not the em-dash fallback.
        assert ">0</span>" in body
