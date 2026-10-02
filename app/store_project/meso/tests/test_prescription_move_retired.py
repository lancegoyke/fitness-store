"""``prescription_move`` is retired (#578 Q2a): a move is delete + re-add.

Two things are pinned here:

1. The endpoint is really gone: its URL name no longer reverses and a POST to
   the old literal path is a 404.
2. Legacy undo/redo still works. Old plans may carry a ``Moved X``
   ``PlanAction`` recorded by the retired endpoint; its snapshot holds the
   slot's old ``session_slot_id``/``order``, and ``restore_plan_snapshot``
   re-points the ``ExerciseSlot`` generically. Those tests rebuild the legacy
   state straight in the ORM (as old prod data looks): the action recorded
   first, then the slot re-pointed with dense renumbering — exactly what the
   endpoint did — and never touch the athlete's ``LoggedSet`` rows.
"""

import json

import pytest
from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.history import record_plan_action
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import PlanAction

from ._helpers import legacy_move_exercise_to_session
from .test_designer_reorder import seed_two_sessions

pytestmark = pytest.mark.django_db


def _post(client, name, plan):
    return client.post(
        reverse(name, kwargs={"plan_id": plan.pk}),
        data=json.dumps({}),
        content_type="application/json",
    )


class TestEndpointIsGone:
    def test_the_url_name_no_longer_reverses(self):
        with pytest.raises(NoReverseMatch):
            reverse("meso:api_prescription_move", kwargs={"plan_id": 1, "pk": 1})

    def test_a_post_to_the_old_path_is_a_404(self, client):
        plan, _week, session_a, session_b, p0, _p1, _q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            f"/meso/api/plan/{plan.pk}/prescription/{p0.pk}/move/",
            data=json.dumps({"session_id": session_b.pk, "index": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        p0.exercise_slot.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_a.session_slot_id


def _legacy_moved_plan():
    """A plan carrying a legacy ``Moved`` action: ``p0`` now sits on day B.

    The action is recorded BEFORE the re-point (its snapshot is the
    pre-move state), as the retired endpoint did. A ``LoggedSet`` on the cell
    stays on day A's log.
    """
    plan, _week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
    log = SessionLogFactory(
        session=session_a, athlete=plan.relationship.athlete, date=timezone.localdate()
    )
    logged = LoggedSetFactory(session_log=log, prescription=p0)
    record_plan_action(plan, f"Moved {p0.name}")
    legacy_move_exercise_to_session(p0, session_b, index=0)
    return plan, session_a, session_b, p0, p1, q0, logged


def _logged_state(logged):
    logged.refresh_from_db()
    return (
        logged.pk,
        logged.prescription_id,
        logged.session_log_id,
        logged.exercise_slot_id,
        logged.load,
        logged.reps,
    )


class TestLegacyMoveActionStillUndoesAndRedoes:
    def test_undo_then_redo_round_trips_the_slot_and_leaves_logged_sets_alone(
        self, client
    ):
        plan, session_a, session_b, p0, p1, q0, logged = _legacy_moved_plan()
        es = p0.exercise_slot
        es.refresh_from_db()
        # Precondition: the legacy state really is "moved".
        assert es.session_slot_id == session_b.session_slot_id
        before = _logged_state(logged)

        client.force_login(plan.relationship.coach)
        resp = _post(client, "meso:api_plan_undo", plan)
        assert resp.status_code == 200
        es.refresh_from_db()
        assert es.session_slot_id == session_a.session_slot_id
        assert es.order == 0
        assert PlanAction.objects.filter(
            plan=plan, stack=PlanAction.Stack.REDO
        ).exists()
        assert _logged_state(logged) == before

        resp = _post(client, "meso:api_plan_redo", plan)
        assert resp.status_code == 200
        es.refresh_from_db()
        assert es.session_slot_id == session_b.session_slot_id
        assert es.order == 0
        assert _logged_state(logged) == before

    def test_undo_when_the_moved_row_was_soft_deleted_afterwards(self, client):
        plan, session_a, _session_b, p0, _p1, _q0, logged = _legacy_moved_plan()
        es = p0.exercise_slot
        ExerciseSlot.objects.filter(pk=es.pk).update(deleted_at=timezone.now())
        before = _logged_state(logged)

        client.force_login(plan.relationship.coach)
        resp = _post(client, "meso:api_plan_undo", plan)
        assert resp.status_code == 200  # not a 500

        # The snapshot predates the delete, so undo restores the row live, back
        # on its source day: one consistent state, not half-moved/half-deleted.
        es.refresh_from_db()
        assert es.deleted_at is None
        assert es.session_slot_id == session_a.session_slot_id
        assert _logged_state(logged) == before
