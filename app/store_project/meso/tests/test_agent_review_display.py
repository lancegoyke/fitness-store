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
    cleaned = _clean(
        plan,
        _blank(
            kind="swap",
            new_name="Box Squat",
            new_load="",
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


@pytest.mark.parametrize(
    ("kind", "cell_text", "value", "model_after", "written"),
    [
        ("progress", "3x5 @ 225", {"new_load": "230"}, "3 x 5, 230", "3x5 @ 230"),
        (
            "progress",
            "3 x 5, RPE 8, 225",
            {"new_load": "230"},
            "3x5 @ 230",
            "3 x 5, RPE 8, 230",
        ),
        ("volume", "3x5 @ 225", {"new_sets": "4"}, "4 x 5, 225", "4x5 @ 225"),
    ],
)
def test_card_after_is_exactly_what_apply_writes(
    kind, cell_text, value, model_after, written
):
    """605.9c: the model's own rendering never reaches the card, the write does."""
    from store_project.meso.agent import apply as agent_apply

    plan, _, cell = make_plan()
    cell.text = cell_text
    cell.save()
    cleaned = _clean(
        plan,
        _blank(
            kind=kind,
            prescription_id=cell.pk,
            after=model_after,
            **{"new_load": "", **value},
        ),
    )
    batch = AgentProposalBatch.objects.create(
        plan=plan, coach=plan.relationship.coach, instruction="x"
    )
    change = ProposedChange.objects.create(batch=batch, **cleaned)
    agent_apply.apply_change(change)
    cell.refresh_from_db()
    assert cleaned["after"] == written
    assert cell.text == written


def test_session_volume_card_is_not_the_models_wording():
    plan, session, cell = make_plan()
    cell.text = "3x5 @ 225"
    cell.save()
    cleaned = _clean(
        plan,
        _blank(
            kind="volume",
            session_id=session.pk,
            new_load="",
            new_sets="4",
            after="4 sets total",
        ),
    )
    assert cleaned["after"] == "4 sets on every exercise"


def test_an_edit_too_long_for_the_card_starts_rejected_not_truncated():
    plan, _, cell = make_plan()
    cell.text = "3x5 " + ("a" * 246) + " @ 225"
    cell.save()
    cleaned = _clean(plan, _blank(prescription_id=cell.pk, new_load="230"))
    assert cleaned["status"] == ProposedChange.Status.REJECTED


def test_overlong_edit_never_stores_a_truncated_after():
    plan, _, cell = make_plan()
    cell.text = "3x5 " + ("a" * 246) + " @ 225"
    cell.save()
    cleaned = _clean(
        plan, _blank(prescription_id=cell.pk, new_load="230", after="3x5 @ 230")
    )
    assert cleaned["after"] == ""


# --- #694: a percent cell progresses in percent -----------------------------


@pytest.mark.parametrize(
    ("cell_text", "new_load", "written"),
    [
        ("3x5 @ 80% 1RM", "82.5%", "3x5 @ 82.5% 1RM"),
        ("3x5 @ 80%", "82.5", "3x5 @ 82.5%"),
        ("3 x 5, RPE 8, 80% 1RM", "82.5%", "3 x 5, RPE 8, 82.5% 1RM"),
        ("3x5 @ 80% of 1RM\nfelt easy", "85", "3x5 @ 85% of 1RM\nfelt easy"),
    ],
)
def test_a_percent_cell_progresses_in_place_and_the_card_matches(
    cell_text, new_load, written
):
    from store_project.meso.agent import apply as agent_apply

    plan, _, cell = make_plan()
    cell.text = cell_text
    cell.save()
    cleaned = _clean(plan, _blank(prescription_id=cell.pk, new_load=new_load))
    assert "status" not in cleaned
    batch = AgentProposalBatch.objects.create(
        plan=plan, coach=plan.relationship.coach, instruction="x"
    )
    change = ProposedChange.objects.create(batch=batch, **cleaned)
    agent_apply.apply_change(change)
    cell.refresh_from_db()
    assert cell.text.split("\n")[0] == cleaned["after"] == written.split("\n")[0]
    assert cell.text == written


def test_sets_edit_on_a_percent_cell_keeps_the_percent():
    from store_project.meso.agent import apply as agent_apply

    plan, _, cell = make_plan()
    cell.text = "3x5 @ 80% 1RM"
    cell.save()
    cleaned = _clean(
        plan, _blank(kind="volume", prescription_id=cell.pk, new_load="", new_sets="4")
    )
    assert cleaned["after"] == "4x5 @ 80% 1RM"
    assert agent_apply.recomposed_text(cell, "sets", "4") == "4x5 @ 80% 1RM"


@pytest.mark.parametrize("new_load", ["230", "100 lb", "230 kg"])
@pytest.mark.parametrize("cell_text", ["3x5 @ 80% 1RM", "3x5 @ 80%"])
def test_an_absolute_load_on_a_percent_cell_is_rejected_never_appended(
    cell_text, new_load
):
    from store_project.meso.agent import apply as agent_apply

    plan, _, cell = make_plan()
    cell.text = cell_text
    cell.save()
    cleaned = _clean(
        plan,
        _blank(prescription_id=cell.pk, new_load=new_load, after=f"{cell_text}, 230"),
    )
    assert cleaned["status"] == ProposedChange.Status.REJECTED
    assert "written as a %1RM; the change gives a weight" in cleaned["rationale"]
    assert cleaned["before"] == cell_text
    assert cleaned["after"] == ""  # nothing lands, so the card shows nothing
    # Even if the coach flips it to Approved, apply writes nothing.
    assert agent_apply.recomposed_text(cell, "load", new_load) is None
    batch = AgentProposalBatch.objects.create(
        plan=plan, coach=plan.relationship.coach, instruction="x"
    )
    change = ProposedChange.objects.create(batch=batch, **cleaned)
    agent_apply.apply_change(change)
    cell.refresh_from_db()
    assert cell.text == cell_text
