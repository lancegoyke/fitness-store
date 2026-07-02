"""Phase 2 PR B — designer.html swaps from Alpine to the React island.

See ``docs/archive/meso/designer-framework-plan.md`` and
``frontend/designer/CONTRACT.md``. The template stops rendering the whole UI
server-side: it now emits only the
mount point (``#meso-designer-root``) plus the same hydration payloads the
Alpine build already injected (``meso-plan-data`` / ``meso-chat-thread`` /
``meso-csrf``), a new ``meso-designer-flags`` payload that replaces the
template's server-side ``{% if is_sandbox %}`` / ``{% elif can_use_agent %}``
gate with data the island renders client-side, and the built
``dist/designer.js`` / ``dist/designer.css`` island bundle in place of
``meso.js`` / ``alpine.min.js``. These guard the server seam only — the
island's own rendering behavior is covered by the vitest suite under
``frontend/designer/``.
"""

import pytest
from django.urls import reverse

from store_project.meso import sandbox as meso_sandbox
from store_project.meso.tests.test_designer_save import seed_plan

pytestmark = pytest.mark.django_db


def render_designer(client, plan):
    client.force_login(plan.relationship.coach)
    resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
    assert resp.status_code == 200
    return resp.content.decode()


class TestIslandMountPoint:
    def test_designer_renders_the_mount_div(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert 'id="meso-designer-root"' in body

    def test_mount_div_carries_the_design_tokens(self, client):
        # The island's CSS only *consumes* these custom properties
        # (frontend/designer/src/designer.css's header comment) — the mount
        # div still defines the values, same as the old Alpine root div did.
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        for token in (
            "--bg:#f4f4f5",
            "--ink:#0a0a0a",
            "--accent:#31759d",
            "--accent-deep:#1f516b",
            "--ok:#2f8f56",
        ):
            assert token in body

    def test_designer_loads_the_built_island_bundle(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert '<script type="module" src="/static/js/dist/designer.js">' in body
        assert '<link rel="stylesheet" href="/static/js/dist/designer.css">' in body

    def test_designer_no_longer_loads_alpine_or_meso_js(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        # Split across two assertions (rather than one literal combined
        # string) so this check doesn't itself trip a repo-wide grep for the
        # exact retired Alpine root-div directive (a project gate).
        assert "x-data=" not in body
        assert "meso()" not in body
        assert "js/meso.js" not in body
        assert "alpine.min.js" not in body


class TestPlanDataStillInjected:
    """The island hydrates from the same payloads the Alpine build used."""

    def test_plan_data_chat_thread_and_csrf_still_present(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert 'id="meso-plan-data"' in body
        assert 'id="meso-chat-thread"' in body
        assert 'id="meso-csrf"' in body


class TestDesignerFlagsPayload:
    """New ``meso-designer-flags`` json_script (CONTRACT.md).

    Replaces the template's server-side sandbox/agent-allowance conditionals.
    """

    def test_flags_json_script_present_with_contract_keys(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert 'id="meso-designer-flags"' in body
        for key in (
            '"is_sandbox"',
            '"can_use_agent"',
            '"agent_allowance"',
            '"signup_url"',
            '"price_summary"',
        ):
            assert key in body

    def test_non_sandbox_coach_gets_is_sandbox_false(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert '"is_sandbox": false' in body

    def test_sandbox_coach_gets_is_sandbox_true(self, client):
        user = meso_sandbox.create_sandbox()
        client.force_login(user)
        resp = client.get(reverse("meso:designer"), follow=True)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert '"is_sandbox": true' in body

    def test_signup_url_resolves_the_sandbox_signup_route(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert reverse("meso:sandbox_signup") in body
