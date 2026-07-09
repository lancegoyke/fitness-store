"""Changes-since-last-delivery diff (the deferred "full diff UI").

Delivering a week records a ``WeekDelivery`` snapshot (``serialize_week_snapshot``).
When a coach comes back to re-deliver, the deliver screen should show **what
changed since the last delivery** — a diff of the week's live grid against the
most recent delivered snapshot. The snapshot data was always captured; this is
the UI that finally reads it (persistence-plan open assumption #3).

Three seams, mirroring the slice discipline:

- ``diff_week_snapshots`` — the pure diff over two snapshot payloads (matched by
  stable pks): added / removed / changed exercise rows grouped by session, whole
  sessions added/removed, and week-meta changes. Exception-aware (P2 → P3): a
  one-week ``skipped`` flip surfaces, but skipped placeholder rows (the
  "add-this-week" seed) never manufacture phantom added/removed/changed noise;
- ``presenters.deliver_screen`` — block delivery, so every live week of the
  target block carries its OWN ``changes`` (``None`` on a first delivery; the
  diff on a re-delivery), folded up into block-level ``is_redelivery`` /
  ``has_changes``;
- ``DeliverView`` — the screen renders each week's diff on a re-delivery.
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
from ._helpers import make_slot
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
        "skipped": False,
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

    # -- one-week skip / add-this-week exceptions (P2 → P3 carry-over) -------- #

    def test_skip_applied_is_a_skipped_field_change(self):
        # Applying a one-week skip after delivery changes what she trains, so it
        # surfaces as a ``skipped`` field flip (off → on).
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", skipped=False)])])
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", skipped=True)])])
        diff = diff_week_snapshots(cur, prev)
        assert diff["has_changes"] is True
        (sess,) = diff["sessions"]
        (changed,) = sess["changed"]
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["skipped"] == (False, True)

    def test_skip_lifted_is_a_skipped_field_change(self):
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", skipped=True)])])
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat", skipped=False)])])
        diff = diff_week_snapshots(cur, prev)
        (sess,) = diff["sessions"]
        (changed,) = sess["changed"]
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["skipped"] == (True, False)

    def test_added_skipped_placeholder_is_not_reported_as_added(self):
        # The "add-this-week" action seeds a skipped placeholder cell (a fresh pk)
        # in every non-target week. The athlete has no new work there, so a skipped
        # row absent from the prior snapshot must NOT show as +added.
        prev = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        cur = _snap(
            [
                _session(
                    1,
                    1,
                    "Lower",
                    [_presc(10, "Squat"), _presc(11, "RDL", skipped=True)],
                )
            ]
        )
        diff = diff_week_snapshots(cur, prev)
        assert diff["sessions"] == []
        assert diff["has_changes"] is False

    def test_removed_skipped_placeholder_is_not_reported_as_removed(self):
        # A skipped placeholder the athlete never trained, now gone, is not a
        # −removed change from her perspective.
        prev = _snap(
            [
                _session(
                    1,
                    1,
                    "Lower",
                    [_presc(10, "Squat"), _presc(11, "RDL", skipped=True)],
                )
            ]
        )
        cur = _snap([_session(1, 1, "Lower", [_presc(10, "Squat")])])
        diff = diff_week_snapshots(cur, prev)
        assert diff["sessions"] == []
        assert diff["has_changes"] is False

    def test_doubly_skipped_row_with_numeric_edit_reports_no_change(self):
        # Skipped in BOTH snapshots → trained in neither delivery, so a numeric
        # edit to it is not an athlete-facing change.
        prev = _snap(
            [_session(1, 1, "Lower", [_presc(10, "Squat", skipped=True, load="100")])]
        )
        cur = _snap(
            [_session(1, 1, "Lower", [_presc(10, "Squat", skipped=True, load="150")])]
        )
        diff = diff_week_snapshots(cur, prev)
        assert diff["sessions"] == []
        assert diff["has_changes"] is False


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
        assert deliver["has_changes"] is False
        # Every per-week entry carries its own (absent) diff on a first delivery.
        assert all(w["changes"] is None for w in deliver["weeks"])
        assert all(w["is_redelivery"] is False for w in deliver["weeks"])

    def test_screen_reports_block_shape(self):
        plan, week1, _, _ = seed_plan()
        WeekFactory(mesocycle=plan.mesocycles.first(), index=2, is_current=False)
        deliver = presenters.deliver_screen(plan)["deliver"]
        assert deliver["week_count"] == 2
        assert deliver["block_name"] == "Hypertrophy"
        assert deliver["week_id"] == week1.pk

    def test_redelivery_after_edit_surfaces_the_change(self):
        plan, week, _, presc = seed_plan(load="111")
        record_delivery(week)
        presc.load = "222"
        presc.save(update_fields=["load"])

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        assert deliver["has_changes"] is True
        (wk,) = [w for w in deliver["weeks"] if w["id"] == week.pk]
        assert wk["is_redelivery"] is True
        changes = wk["changes"]
        assert changes is not None
        assert changes["has_changes"] is True
        (sess,) = changes["sessions"]
        (changed,) = sess["changed"]
        assert changed["name"] == "Box Squat"
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["load"] == ("111", "222")

    def test_redelivery_after_skip_surfaces_the_change(self):
        # A one-week skip applied after delivery is athlete-facing (trained
        # before, not now) → it surfaces on re-delivery as a ``skipped`` flip.
        plan, week, _, presc = seed_plan()
        record_delivery(week)
        presc.skipped = True
        presc.save(update_fields=["skipped"])

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        assert deliver["has_changes"] is True
        (wk,) = deliver["weeks"]
        (sess,) = wk["changes"]["sessions"]
        (changed,) = sess["changed"]
        fields = {f["field"]: (f["before"], f["after"]) for f in changed["fields"]}
        assert fields["skipped"] == (False, True)

    def test_redelivery_with_no_edits_reports_no_changes(self):
        plan, week, _, _ = seed_plan()
        record_delivery(week)

        deliver = presenters.deliver_screen(plan)["deliver"]

        assert deliver["is_redelivery"] is True
        assert deliver["has_changes"] is False
        (wk,) = deliver["weeks"]
        assert wk["changes"] is not None
        assert wk["changes"]["has_changes"] is False

    def test_each_week_carries_its_own_diff(self):
        # Block delivery: every live week diffs against ITS OWN last-delivered
        # snapshot — an edit on the built-ahead week surfaces on that week only.
        plan, week1, _, _ = seed_plan()
        meso = plan.mesocycles.first()
        week2 = WeekFactory(mesocycle=meso, index=2, is_current=False)
        session2 = day(week2, day_number=2, name="Upper")
        presc2 = presc_(session2, name="Bench", sets="3", reps="5", load="80")
        record_delivery(week2)
        presc2.load = "90"
        presc2.save(update_fields=["load"])

        deliver = presenters.deliver_screen(plan, week=week2)["deliver"]

        # ?week= only *selects the block* — both weeks are listed.
        assert deliver["week_id"] == week2.pk
        ids = {w["id"] for w in deliver["weeks"]}
        assert ids == {week1.pk, week2.pk}
        (wk2,) = [w for w in deliver["weeks"] if w["id"] == week2.pk]
        (sess,) = wk2["changes"]["sessions"]
        (changed,) = sess["changed"]
        assert changed["name"] == "Bench"
        # week1 was never delivered → no diff of its own.
        (wk1,) = [w for w in deliver["weeks"] if w["id"] == week1.pk]
        assert wk1["changes"] is None

    def test_add_this_week_placeholder_is_not_a_phantom_add_on_other_weeks(self):
        # "add-this-week" creates a new ExerciseSlot + a real cell on the target
        # week and a ``skipped`` placeholder cell on every OTHER live week. On a
        # re-delivery, the placeholder must not surface as a phantom +add on a
        # non-target week the athlete has no new work in.
        plan, week1, session1, _ = seed_plan()
        meso = plan.mesocycles.first()
        week2 = WeekFactory(mesocycle=meso, index=2, is_current=False)
        day(week2, session_slot=session1.session_slot)
        record_delivery(week1)
        record_delivery(week2)

        new_slot = make_slot(session1, name="Face Pull")
        presc_(exercise_slot=new_slot, week=week1, skipped=False)
        presc_(exercise_slot=new_slot, week=week2, skipped=True)

        deliver = presenters.deliver_screen(plan)["deliver"]

        (wk1,) = [w for w in deliver["weeks"] if w["id"] == week1.pk]
        (wk2,) = [w for w in deliver["weeks"] if w["id"] == week2.pk]
        # The target week legitimately gains Face Pull...
        assert wk1["changes"]["has_changes"] is True
        added = [a["label"] for s in wk1["changes"]["sessions"] for a in s["added"]]
        assert any("Face Pull" in lbl for lbl in added)
        # ...but the other week's skipped placeholder is invisible to her.
        assert wk2["changes"]["has_changes"] is False


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

    def test_redelivery_screen_renders_per_week_change_headers(self, client):
        # Two delivered weeks, each edited → each gets its own "Wk N" section.
        plan, week1, _, presc1 = seed_plan(load="111")
        meso = plan.mesocycles.first()
        week2 = WeekFactory(mesocycle=meso, index=2, is_current=False)
        session2 = day(week2, day_number=2, name="Upper")
        presc2 = presc_(session2, name="Bench", sets="3", reps="5", load="80")
        record_delivery(week1)
        record_delivery(week2)
        presc1.load = "222"
        presc1.save(update_fields=["load"])
        presc2.load = "90"
        presc2.save(update_fields=["load"])
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        assert "Wk 1" in body
        assert "Wk 2" in body
        assert "Box Squat" in body
        assert "Bench" in body

    def test_redelivery_screen_renders_a_skip_as_off_to_on(self, client):
        # A ``skipped`` flip renders through the on/off (yesno) branch, never the
        # boolean "— → True" the |default fallback would give.
        plan, week, _, presc = seed_plan()
        record_delivery(week)
        presc.skipped = True
        presc.save(update_fields=["skipped"])
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        assert "Skipped" in body
        assert ">on</span>" in body
        assert ">True</span>" not in body

    def test_redelivery_screen_with_no_edits_says_no_changes(self, client):
        plan, week, _, _ = seed_plan()
        record_delivery(week)
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        assert "No changes" in body

    def test_add_this_week_placeholder_shows_no_phantom_add_on_other_weeks(
        self, client
    ):
        # The skipped placeholder on a non-target week must not render a phantom
        # + add. The target week (Face Pull added) does; the other week reads
        # "No changes".
        plan, week1, session1, _ = seed_plan()
        meso = plan.mesocycles.first()
        week2 = WeekFactory(mesocycle=meso, index=2, is_current=False)
        day(week2, session_slot=session1.session_slot)
        record_delivery(week1)
        record_delivery(week2)
        new_slot = make_slot(session1, name="Face Pull")
        presc_(exercise_slot=new_slot, week=week1, skipped=False)
        presc_(exercise_slot=new_slot, week=week2, skipped=True)
        client.force_login(plan.relationship.coach)

        body = self._screen(client, plan).content.decode()

        # Face Pull surfaces exactly once (the target week's real add), and the
        # non-target week reads as unchanged rather than a phantom add.
        assert body.count("Face Pull") == 1
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
