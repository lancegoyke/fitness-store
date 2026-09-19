"""``MesoDesignerView._phone_fallback`` (issue #508, second slice).

Under 900px `designer.html` hides the React island and shows a
server-rendered message instead — links to the athlete's profile and to
deliver the block on screen, with no JS branch needed (`meso.css`'s
`.meso-designer-fallback` section). This is the server-seam coverage for what
`_phone_fallback` puts into that message's context: the athlete/deliver URLs
for a real plan, neither for a template, and no deliver URL when the block
being viewed has no live week to deliver. `e2e/test_meso_coach_mobile.py`
covers what a phone actually shows.
"""

import pytest
from django.urls import reverse

from store_project.meso.factories import WeekFactory
from store_project.meso.tests.test_designer_save import seed_plan
from store_project.meso.tests.test_template_plans import template_plan

pytestmark = pytest.mark.django_db


def _get_designer(client, plan):
    client.force_login(plan.coach)
    resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
    assert resp.status_code == 200
    return resp


class TestPhoneFallbackForAClientPlan:
    def test_context_has_the_athlete_and_deliver_urls(self, client):
        plan, session, cell = seed_plan()
        resp = _get_designer(client, plan)
        fallback = resp.context["phone_fallback"]
        assert fallback["is_template"] is False
        assert fallback["athlete_name"] == plan.athlete.display_name()
        assert fallback["athlete_url"] == reverse(
            "meso:athlete", kwargs={"pk": plan.athlete.pk}
        )
        deliver_url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        assert fallback["deliver_url"] == f"{deliver_url}?week={session.week_id}"

    def test_deliver_url_targets_the_default_blocks_first_live_week(self, client):
        # A second, later week on the same block: the fallback still targets
        # the block's FIRST live week — the same week the island's own
        # deliverHref (and DeliverView's default) target.
        plan, session, cell = seed_plan()
        WeekFactory(mesocycle=session.week.mesocycle, index=2)
        resp = _get_designer(client, plan)
        fallback = resp.context["phone_fallback"]
        deliver_url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        assert fallback["deliver_url"] == f"{deliver_url}?week={session.week_id}"

    def test_no_deliver_url_when_the_default_block_has_no_live_week(self, client):
        # Soft-deleted, like a real removed week (Week.deleted_at) — the
        # block itself still exists, it just has nothing left to deliver.
        plan, session, cell = seed_plan()
        session.week.soft_delete()
        resp = _get_designer(client, plan)
        fallback = resp.context["phone_fallback"]
        assert "deliver_url" not in fallback
        # The athlete link is unaffected — there's still someone to visit.
        assert fallback["athlete_url"] == reverse(
            "meso:athlete", kwargs={"pk": plan.athlete.pk}
        )

    def test_renders_the_fallback_message(self, client):
        plan, session, cell = seed_plan()
        body = _get_designer(client, plan).content.decode()
        assert 'data-testid="designer-fallback"' in body
        assert "Open this on a larger screen to edit the program." in body
        deliver_url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        assert f'href="{deliver_url}?week={session.week_id}"' in body
        assert (
            f'href="{reverse("meso:athlete", kwargs={"pk": plan.athlete.pk})}"' in body
        )


class TestPhoneFallbackForATemplate:
    def test_context_has_neither_athlete_nor_deliver_url(self, client):
        plan, cell = template_plan()
        resp = _get_designer(client, plan)
        fallback = resp.context["phone_fallback"]
        assert fallback["is_template"] is True
        assert "athlete_url" not in fallback
        assert "deliver_url" not in fallback

    def test_renders_the_templates_link_not_an_athlete_link(self, client):
        plan, cell = template_plan()
        body = _get_designer(client, plan).content.decode()
        assert 'data-testid="designer-fallback"' in body
        assert f'href="{reverse("meso:template_library")}"' in body
        assert "'s profile</a>" not in body
