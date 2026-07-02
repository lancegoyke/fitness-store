"""Phase 1 — undo/redo backend (the ``PlanAction`` op-log).

The designer needs plan-wide undo/redo (``docs/meso/designer-framework-plan.md``
Decision 2 + Phase 1), built on Phase 0's soft-delete: every mutating designer
endpoint records ONE ``PlanAction`` (stack ``undo``, monotonically increasing
``seq``, a short human ``label``, and a plan-wide ``snapshot`` of the editable
state taken BEFORE the mutation). ``POST api/plan/<id>/undo/`` pops the max-seq
undo row, pushes the mirror-image redo row (same seq+label, snapshot = current
state), and restores the popped snapshot; ``redo/`` is the mirror (pops the
min-seq redo row). Restore only ever flips fields and ``deleted_at`` — nothing
is hard-deleted or recreated, so an undone add redoes onto the SAME pk and an
undone day-delete resurfaces with its athlete's ``SessionLog``/``LoggedSet``
rows untouched. ``serialize_plan`` grows a ``history`` key so the designer's
buttons stay accurate after every ``applyPlanData``.

Covers, per the Phase 1 spec:

- recording: each mutating endpoint records exactly one UNDO ``PlanAction``
  with consecutive ``seq`` (and ``coach_set_one_rm`` / ``plan_deliver`` record
  nothing — athlete data and delivery stamps are not editable state);
- snapshot spot-checks (pre-mutation prescription values; the soft-deleted
  week's pk);
- undo/redo round trips for a cell edit, a day delete (logs survive), an added
  exercise (same pk on redo), a per-athlete override, and ``week_set_current``;
- batch_apply records ONE action for the whole batch and undo reverts it all;
- redo invalidation on a fresh mutation; the exact empty-stack error strings;
- the 50-row history cap (oldest trimmed);
- the shared auth matrix (302/403/405/402) and viewed-week preservation;
- the 409 "History unavailable" restore-conflict path;
- ``serialize_plan``'s ``history`` key.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import GroupMembershipFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import ExercisePrescription
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import PrescriptionOverride
from store_project.meso.models import ProposedChange
from store_project.meso.models import SessionLog
from store_project.meso.models import Week
from store_project.meso.serializers import serialize_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

EMPTY_HISTORY = {
    "can_undo": False,
    "can_redo": False,
    "undo_label": None,
    "redo_label": None,
}


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = SessionFactory(week=week, day_number=1, name="Lower")
    presc = ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return plan, week, session, presc


def seed_group_plan():
    """A minimal owned group plan with one current week → session → prescription."""
    group = MesoGroupFactory()
    plan = GroupPlanFactory(group=group, title="Squad Block", status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = SessionFactory(week=week, day_number=1, name="Lower")
    presc = ExercisePrescriptionFactory(session=session, name="Box Squat")
    return group, plan, week, session, presc


def _two_week_plan():
    """A plan with ``week1`` (current) and ``week2`` (non-current)."""
    link = CoachAthleteFactory()
    plan = link.create_plan()
    meso = plan.mesocycles.get()
    week1 = meso.weeks.get(index=1)
    week2 = meso.append_week()
    return link, plan, week1, week2


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def redo_url(plan):
    return reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk})


def patch_url(plan, presc):
    return reverse(
        "meso:api_prescription_patch", kwargs={"plan_id": plan.pk, "pk": presc.pk}
    )


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def patch(client, plan, presc, **fields):
    resp = post_json(client, patch_url(plan, presc), fields)
    assert resp.status_code == 200
    return resp


def undo_actions(plan):
    return PlanAction.objects.filter(plan=plan, stack=PlanAction.Stack.UNDO)


def redo_actions(plan):
    return PlanAction.objects.filter(plan=plan, stack=PlanAction.Stack.REDO)


def _envelope_keys(body):
    return {"program", "weeks", "viewing", "phases"}.issubset(body.keys())


def _live_exercise_ids(plan, week=None):
    data = serialize_plan(plan, week=week)
    return [e["id"] for day in data["program"] for e in day["exercises"]]


# ---------------------------------------------------------------------------
# Recording — one UNDO action per mutating endpoint, none for the excluded ones
# ---------------------------------------------------------------------------


class TestRecording:
    def test_each_mutating_endpoint_records_one_undo_action_with_increasing_seq(
        self, client
    ):
        plan, week1, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)

        # 1. prescription_patch
        patch(client, plan, presc, load="75")
        assert undo_actions(plan).count() == 1

        # 2. session_add_exercise
        resp = client.post(
            reverse(
                "meso:api_session_add_exercise",
                kwargs={"plan_id": plan.pk, "pk": session.pk},
            )
        )
        assert resp.status_code == 201
        added_presc_id = resp.json()["prescription"]["id"]
        assert undo_actions(plan).count() == 2

        # 3. session_add (a new training day on the current week)
        resp = client.post(reverse("meso:api_session_add", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 201
        added_session_id = resp.json()["session"]["id"]
        assert undo_actions(plan).count() == 3

        # 4. week_add
        resp = client.post(reverse("meso:api_week_add", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 201
        added_week_id = resp.json()["viewing"]
        assert undo_actions(plan).count() == 4

        # 5. week_set_current (flip the pointer onto the new week)
        resp = client.post(
            reverse(
                "meso:api_week_set_current",
                kwargs={"plan_id": plan.pk, "week_id": added_week_id},
            )
        )
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 5

        # 6–8. the three Phase 0 deletes
        resp = client.post(
            reverse(
                "meso:api_prescription_delete",
                kwargs={"plan_id": plan.pk, "pk": added_presc_id},
            )
        )
        assert resp.status_code == 200
        resp = client.post(
            reverse(
                "meso:api_session_delete",
                kwargs={"plan_id": plan.pk, "pk": added_session_id},
            )
        )
        assert resp.status_code == 200
        resp = client.post(
            reverse(
                "meso:api_week_delete",
                kwargs={"plan_id": plan.pk, "week_id": week1.pk},
            )
        )
        assert resp.status_code == 200

        actions = list(undo_actions(plan).order_by("seq"))
        assert len(actions) == 8
        seqs = [a.seq for a in actions]
        # No undos in between, so the allocator (max seq + 1) yields consecutive
        # seqs; every recorded row lands on the undo stack, redo stays empty.
        assert seqs == list(range(seqs[0], seqs[0] + 8))
        assert redo_actions(plan).count() == 0
        for action in actions:
            assert isinstance(action.snapshot, dict)
            assert action.snapshot  # never an empty snapshot
            assert action.label  # every action carries a human label
        # The three deletes are the last three actions; their labels read as
        # deletions (loose pin — exact wording is the implementer's).
        for action in actions[5:]:
            assert "Deleted" in action.label

    def test_patch_snapshot_captures_pre_mutation_prescription_values(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="90", rpe="9")

        action = undo_actions(plan).get()
        rows = action.snapshot["prescriptions"]
        entry = next(r for r in rows if r["pk"] == presc.pk)
        # The snapshot holds the state BEFORE the mutation.
        assert entry["name"] == "Box Squat"
        assert entry["load"] == "70"
        assert entry["rpe"] == "7"
        assert entry["deleted_at"] is None

    def test_week_delete_snapshot_contains_the_doomed_weeks_pk(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        resp = client.post(
            reverse(
                "meso:api_week_delete",
                kwargs={"plan_id": plan.pk, "week_id": week2.pk},
            )
        )
        assert resp.status_code == 200

        action = undo_actions(plan).order_by("-seq").first()
        weeks = action.snapshot["weeks"]
        entry = next(w for w in weeks if w["pk"] == week2.pk)
        # Pre-mutation: the week was still live when the snapshot was taken.
        assert entry["deleted_at"] is None

    def test_coach_set_one_rm_records_nothing(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            reverse(
                "meso:api_coach_set_one_rm",
                kwargs={"plan_id": plan.pk, "pk": presc.pk},
            ),
            {"value": "140"},
        )
        assert resp.status_code == 200
        assert PlanAction.objects.filter(plan=plan).count() == 0

    def test_plan_deliver_records_nothing(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
        assert resp.status_code == 201
        assert PlanAction.objects.filter(plan=plan).count() == 0


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/undo/ — restore + the stack dance
# ---------------------------------------------------------------------------


class TestUndoPrescriptionPatch:
    def test_undo_restores_prior_cell_values_and_returns_envelope(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="75", rpe="8", note="felt easy")
        presc.refresh_from_db()
        assert presc.load == "75"  # sanity: the edit landed

        popped = undo_actions(plan).get()
        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        assert "history" in body
        assert body["history"]["can_redo"] is True
        assert body["history"]["can_undo"] is False

        presc.refresh_from_db()
        assert presc.load == "70"
        assert presc.rpe == "7"
        assert presc.note == ""

        # The popped undo row moved to the redo stack with the SAME seq + label.
        assert undo_actions(plan).count() == 0
        redo_row = redo_actions(plan).get()
        assert redo_row.seq == popped.seq
        assert redo_row.label == popped.label

    def test_undo_bumps_plan_modified(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="75")
        plan.refresh_from_db()
        before = plan.modified
        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        plan.refresh_from_db()
        assert plan.modified > before


class TestUndoDeleteDay:
    def test_undo_restores_the_day_with_its_logs_intact(self, client):
        plan, week, session, presc = seed_plan()
        log = SessionLogFactory(session=session, athlete=plan.athlete)
        logged_set = LoggedSetFactory(session_log=log, prescription=presc)
        client.force_login(plan.relationship.coach)

        resp = client.post(
            reverse(
                "meso:api_session_delete",
                kwargs={"plan_id": plan.pk, "pk": session.pk},
            )
        )
        assert resp.status_code == 200
        session.refresh_from_db()
        assert session.deleted_at is not None

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200

        # The day is live again…
        session.refresh_from_db()
        assert session.deleted_at is None
        data = serialize_plan(plan)
        assert session.pk in [d["id"] for d in data["program"]]
        # …and the athlete's history survived the whole round trip, still
        # pointing at the SAME session (soft delete never touched it).
        log.refresh_from_db()
        assert log.session_id == session.pk
        logged_set.refresh_from_db()
        assert logged_set.session_log_id == log.pk
        assert logged_set.prescription_id == presc.pk
        assert SessionLog.objects.filter(session=session).count() == 1
        assert LoggedSet.objects.filter(session_log=log).count() == 1


class TestUndoAddExercise:
    def test_undo_soft_deletes_the_added_row_and_redo_revives_the_same_pk(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            reverse(
                "meso:api_session_add_exercise",
                kwargs={"plan_id": plan.pk, "pk": session.pk},
            )
        )
        assert resp.status_code == 201
        new_pk = resp.json()["prescription"]["id"]
        rows_after_add = ExercisePrescription.objects.count()

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        # Soft-deleted, not hard-deleted: the row survives with the flag set…
        added = ExercisePrescription.objects.get(pk=new_pk)
        assert added.deleted_at is not None
        # …and drops out of the serialized plan.
        assert new_pk not in _live_exercise_ids(plan, week=week)

        resp = client.post(redo_url(plan))
        assert resp.status_code == 200
        # Redo revives the SAME row — never a recreated one.
        added.refresh_from_db()
        assert added.deleted_at is None
        assert new_pk in _live_exercise_ids(plan, week=week)
        assert ExercisePrescription.objects.count() == rows_after_add


class TestRoundTrip:
    def test_edit_undo_redo_leaves_db_identical_to_post_edit(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="82.5", rpe="8", note="top set")
        presc.refresh_from_db()
        post_edit = (
            presc.name,
            presc.sets,
            presc.reps,
            presc.load,
            presc.rpe,
            presc.note,
            presc.deleted_at,
        )

        assert client.post(undo_url(plan)).status_code == 200
        presc.refresh_from_db()
        assert presc.load == "70"  # sanity: the undo actually reverted

        resp = client.post(redo_url(plan))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)

        presc.refresh_from_db()
        assert (
            presc.name,
            presc.sets,
            presc.reps,
            presc.load,
            presc.rpe,
            presc.note,
            presc.deleted_at,
        ) == post_edit
        # The stacks mirror back: one undoable action again, nothing redoable.
        assert undo_actions(plan).count() == 1
        assert redo_actions(plan).count() == 0
        assert body["history"]["can_undo"] is True
        assert body["history"]["can_redo"] is False


class TestRedoInvalidation:
    def test_fresh_mutation_after_undo_clears_the_redo_stack(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="75")
        assert client.post(undo_url(plan)).status_code == 200
        assert redo_actions(plan).count() == 1

        # A fresh mutation forks history — the redo stack is dropped.
        patch(client, plan, presc, rpe="9")
        assert redo_actions(plan).count() == 0

        resp = client.post(redo_url(plan))
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Nothing to redo"


class TestEmptyStacks:
    def test_undo_on_empty_stack_400s_with_the_exact_error(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(undo_url(plan))
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Nothing to undo"

    def test_redo_on_empty_stack_400s_with_the_exact_error(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(redo_url(plan))
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Nothing to redo"


class TestHistoryCap:
    def test_55_mutations_keep_exactly_50_undo_rows_trimming_the_oldest(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)

        patch(client, plan, presc, load="1")
        first_seq = undo_actions(plan).get().seq
        for i in range(2, 56):  # 54 more → 55 total
            patch(client, plan, presc, load=str(i))

        seqs = sorted(undo_actions(plan).values_list("seq", flat=True))
        assert len(seqs) == 50
        # The oldest five (lowest seqs) were trimmed; the newest 50 remain.
        assert seqs[0] == first_seq + 5
        assert seqs[-1] == first_seq + 54
        assert first_seq not in seqs


# ---------------------------------------------------------------------------
# batch_apply — one snapshot for the whole batch
# ---------------------------------------------------------------------------


class TestBatchApplyUndo:
    def test_batch_apply_records_one_action_and_undo_reverts_every_change(self, client):
        plan, week, session, presc = seed_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.relationship.coach)
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Front Squat"},
        )
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.PROGRESS,
            prescription=presc,
            payload={"load": "85"},
        )
        client.force_login(plan.relationship.coach)

        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == 2
        presc.refresh_from_db()
        assert presc.name == "Front Squat"
        assert presc.load == "85"

        # ONE action for the whole batch, labelled as an agent apply.
        actions = list(undo_actions(plan))
        assert len(actions) == 1
        assert "Applied" in actions[0].label

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        # Every change the batch applied is reverted by the single undo.
        presc.refresh_from_db()
        assert presc.name == "Box Squat"
        assert presc.load == "70"


# ---------------------------------------------------------------------------
# prescription_override — reconciled by natural key
# ---------------------------------------------------------------------------


class TestOverrideUndoRedo:
    def test_undo_drops_the_override_and_redo_restores_the_same_values(self, client):
        group, plan, week, session, presc = seed_group_plan()
        membership = GroupMembershipFactory(group=group)
        athlete_id = str(membership.relationship.athlete_id)
        client.force_login(group.coach)

        resp = post_json(
            client,
            reverse(
                "meso:api_prescription_override",
                kwargs={"plan_id": plan.pk, "pk": presc.pk},
            ),
            {"athlete": athlete_id, "swap": "Box Squat Variant", "load_pct": 90},
        )
        assert resp.status_code == 200
        assert PrescriptionOverride.objects.filter(
            membership=membership, prescription=presc
        ).exists()
        assert undo_actions(plan).count() == 1

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        assert not PrescriptionOverride.objects.filter(
            membership=membership, prescription=presc
        ).exists()

        resp = client.post(redo_url(plan))
        assert resp.status_code == 200
        override = PrescriptionOverride.objects.get(
            membership=membership, prescription=presc
        )
        assert override.swap_name == "Box Squat Variant"
        assert override.load_pct == 90


# ---------------------------------------------------------------------------
# week_set_current — undo restores the previous pointer
# ---------------------------------------------------------------------------


class TestWeekSetCurrentUndo:
    def test_undo_makes_the_previous_week_current_again(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        resp = client.post(
            reverse(
                "meso:api_week_set_current",
                kwargs={"plan_id": plan.pk, "week_id": week2.pk},
            )
        )
        assert resp.status_code == 200
        week2.refresh_from_db()
        assert week2.is_current is True

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week1.is_current is True
        assert week2.is_current is False


# ---------------------------------------------------------------------------
# Auth — mirrors every other designer mutation
# ---------------------------------------------------------------------------


class TestAuth:
    def test_requires_login(self, client):
        plan, week, session, presc = seed_plan()
        for url in (undo_url(plan), redo_url(plan)):
            resp = client.post(url)
            assert resp.status_code == 302
            assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        assert client.get(undo_url(plan)).status_code == 405
        assert client.get(redo_url(plan)).status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(UserFactory())
        assert client.post(undo_url(plan)).status_code == 403
        assert client.post(redo_url(plan)).status_code == 403

    def test_over_limit_coach_gets_402(self, client):
        # Mirrors test_billing_enforcement: an older kept link plus this plan's
        # newer one pushes the free coach over the cap, freezing this plan.
        coach = UserFactory()
        CoachAthleteFactory(coach=coach)  # older link → kept
        plan, week, session, presc = seed_plan(coach=coach)  # newer → suspended
        client.force_login(coach)
        resp = client.post(undo_url(plan))
        assert resp.status_code == 402
        assert resp.json()["over_limit"] is True
        assert client.post(redo_url(plan)).status_code == 402


# ---------------------------------------------------------------------------
# Viewed-week preservation
# ---------------------------------------------------------------------------


class TestViewedWeekPreservation:
    def test_undo_pins_the_response_to_the_posted_week_when_still_live(self, client):
        link, plan, week1, week2 = _two_week_plan()
        presc = week1.sessions.first().prescriptions.first()
        client.force_login(link.coach)
        patch(client, plan, presc, load="99")

        resp = post_json(client, undo_url(plan), {"week_id": week2.pk})
        assert resp.status_code == 200
        body = resp.json()
        # The coach was viewing week 2 — the reply keeps them there.
        assert body["viewing"] == week2.pk
        presc.refresh_from_db()
        assert presc.load == ""  # the scaffolded row's pre-edit load

    def test_undo_falls_back_to_current_when_the_viewed_week_was_un_created(
        self, client
    ):
        plan, week1, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(reverse("meso:api_week_add", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 201
        new_week_id = resp.json()["viewing"]
        assert new_week_id != week1.pk

        # Undoing the add soft-deletes the very week the client was viewing —
        # the reply falls back to the (still current) first week.
        resp = post_json(client, undo_url(plan), {"week_id": new_week_id})
        assert resp.status_code == 200
        assert resp.json()["viewing"] == week1.pk
        undone = Week.objects.get(pk=new_week_id)  # soft-deleted, never hard
        assert undone.deleted_at is not None


# ---------------------------------------------------------------------------
# Restore conflict — a snapshot pk that no longer exists
# ---------------------------------------------------------------------------


class TestRestoreConflict:
    def test_hard_deleted_snapshot_row_makes_undo_a_409(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="75")

        # Simulate history rot: something hard-deleted a snapshotted row out
        # from under the op-log (bypassing soft delete via the queryset).
        ExercisePrescription.objects.filter(pk=presc.pk).delete()

        resp = client.post(undo_url(plan))
        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "History unavailable"
        # The transaction rolled back — the stack is not half-consumed.
        assert undo_actions(plan).count() == 1
        assert redo_actions(plan).count() == 0


# ---------------------------------------------------------------------------
# serialize_plan's history key
# ---------------------------------------------------------------------------


class TestSerializePlanHistory:
    def test_fresh_plan_has_an_empty_history(self, client):
        plan, week, session, presc = seed_plan()
        data = serialize_plan(plan)
        assert data["history"] == EMPTY_HISTORY

    def test_after_an_edit_can_undo_is_true_with_a_label(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        patch(client, plan, presc, load="75")

        history = serialize_plan(plan)["history"]
        assert history["can_undo"] is True
        assert history["can_redo"] is False
        assert isinstance(history["undo_label"], str) and history["undo_label"]
        assert history["redo_label"] is None

    def test_history_rides_existing_serialize_plan_responses(self, client):
        # week_view is one of the endpoints whose reply the client feeds through
        # applyPlanData — history must ride it so the buttons stay accurate.
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(
            reverse(
                "meso:api_week_view",
                kwargs={"plan_id": plan.pk, "week_id": week.pk},
            )
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "history" in body
        assert set(body["history"].keys()) == {
            "can_undo",
            "can_redo",
            "undo_label",
            "redo_label",
        }
