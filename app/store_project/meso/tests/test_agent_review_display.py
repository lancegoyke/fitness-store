"""#647 — the review card must show before → after and the day tag.

``before``/``after``/``day_label`` are display text the model may leave blank
(UAT round 2: a progress on Back Squat came back with both empty, so the card
showed a bare "→" over a pre-approved change). The server derives them from the
program itself at proposal time, and a change whose edit can't be shown starts
Rejected instead of pre-approved.
"""

import pytest
from django.urls import reverse

from store_project.meso.agent import validation
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def _blank(**overrides):
    change = {
        "kind": "progress",
        "title": "Deload Back Squat in Week 2",
        "before": "",
        "after": "",
        "day_label": "",
        "rationale": "Reset the load.",
        "new_load": "225",
    }
    change.update(overrides)
    return change


def _clean(plan, raw):
    cleaned, errors = validation.clean_change(
        raw, plan, mesocycle=plan.mesocycles.first()
    )
    assert errors == []
    return cleaned


def _persist_and_render(client, plan, cleaned):
    batch = AgentProposalBatch.objects.create(
        plan=plan, coach=plan.relationship.coach, instruction="x"
    )
    ProposedChange.objects.create(batch=batch, **cleaned)
    client.force_login(plan.relationship.coach)
    return client.get(reverse("meso:review_batch", args=[batch.pk]))


def test_blank_progress_gets_before_after_and_day_tag(client):
    plan, session, cell = make_plan()
    cell.text = "3x5 @ 235"
    cell.save()
    cleaned = _clean(plan, _blank(prescription_id=cell.pk))
    assert cleaned["before"] == "3x5 @ 235"
    assert "225" in cleaned["after"]
    assert cleaned["day_label"] == str(session)
    assert "status" not in cleaned  # stays pending

    html = _persist_and_render(client, plan, cleaned).content.decode()
    assert "3x5 @ 235" in html
    assert str(session) in html


def test_model_supplied_display_text_is_kept():
    plan, _, cell = make_plan()
    cell.text = "3x5 @ 235"
    cell.save()
    cleaned = _clean(
        plan,
        _blank(
            prescription_id=cell.pk,
            before="old",
            after="new",
            day_label="Day 1 · Lower",
        ),
    )
    assert (cleaned["before"], cleaned["after"]) == ("old", "new")
    assert cleaned["day_label"] == "Day 1 · Lower"


def test_blank_swap_shows_names():
    plan, _, cell = make_plan()
    cleaned = _clean(
        plan,
        _blank(kind="swap", prescription_id=cell.pk, new_load="", new_name="Box Squat"),
    )
    assert cleaned["before"] == "Back Squat"
    assert cleaned["after"] == "Box Squat"


def test_undisplayable_change_starts_rejected_not_approved(client):
    plan, _, cell = make_plan()
    cell.text = "Wave: 5-3-1 on the platform"  # no recoverable sets/reps
    cell.save()
    cleaned = _clean(plan, _blank(prescription_id=cell.pk))
    assert cleaned["before"] == "" and cleaned["after"] == ""
    assert cleaned["status"] == ProposedChange.Status.REJECTED

    html = _persist_and_render(client, plan, cleaned).content.decode()
    assert "'rejected'" in html
    assert "can't show" in html


def test_progress_on_unreadable_cell_starts_rejected_even_with_model_text():
    plan, _, cell = make_plan()
    cell.text = "Wave: 5-3-1 on the platform"
    cell.save()
    cleaned = _clean(
        plan,
        _blank(prescription_id=cell.pk, before="Wave at 200", after="Wave at 225"),
    )
    assert cleaned["status"] == ProposedChange.Status.REJECTED


def test_session_volume_with_no_readable_cells_starts_rejected():
    plan, session, cell = make_plan()
    cell.text = "Wave: 5-3-1 on the platform"
    cell.save()
    cleaned = _clean(
        plan,
        _blank(kind="volume", session_id=session.pk, new_sets="3", new_load=""),
    )
    assert cleaned["status"] == ProposedChange.Status.REJECTED
