"""Issue #388 (Level-1 demo) — the automated-recording seed command.

``seed_demo_recording`` is the deterministic sibling of ``seed_meso_demo``: the
demo-video recorder drives a real browser through the real allauth login form,
so it needs a coach whose credentials are **known in advance**, not a
random throwaway password printed once to stdout. These tests pin its
contract:

- a coach exists with a known, usable password (+ ``CoachProfile`` + a
  **comped** ``CoachSubscription`` — the demo workspace would otherwise trip
  the free-tier seat/agent gates mid-recording);
- the full demo workspace (``meso.demo.load_demo``) is loaded;
- Maya Okonkwo's demo athlete additionally gets a usable, known password (the
  same as the coach's) for the optional athlete-phone-view shot;
- reruns are idempotent and always converge the password back to the known
  value, even if something changed it since the last run;
- reruns **reset** the workspace (``clear_demo`` → ``load_demo``): whatever a
  previous recording layered on top (applied agent batches and their designer
  chat history) is gone, so run N's video never shows run N-1's chat thread;
- ``--json`` prints exactly ``{"coach_email": ..., "athlete_email": ...}`` and
  never the password;
- it refuses to run with ``settings.DEBUG`` off unless ``--force`` is passed.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from store_project.meso.demo import demo_email
from store_project.meso.demo import has_demo
from store_project.meso.management.commands.seed_demo_recording import (
    DEFAULT_COACH_PASSWORD,
)
from store_project.meso.management.commands.seed_demo_recording import Command
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db

COACH_EMAIL = "demo-coach@demo.invalid"


@pytest.fixture(autouse=True)
def _debug_on(settings):
    """Most of this suite needs DEBUG on; ``TestDebugGuard`` flips it off itself."""
    settings.DEBUG = True


def seed(**options):
    call_command("seed_demo_recording", **options)


class TestSeedsCoach:
    def test_creates_coach_with_known_usable_password(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert coach.check_password(DEFAULT_COACH_PASSWORD)
        assert coach.has_usable_password()
        assert coach.name  # presentable on camera

    def test_creates_coach_profile(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert CoachProfile.objects.filter(user=coach).exists()

    def test_comps_the_subscription(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.COMPED
        assert sub.is_active

    def test_custom_email_and_password_args(self):
        seed(email="custom-coach@demo.invalid", password="a-different-pw")
        coach = User.objects.get(email="custom-coach@demo.invalid")
        assert coach.check_password("a-different-pw")

    def test_env_vars_override_defaults(self, monkeypatch):
        monkeypatch.setenv("MESO_DEMO_COACH_EMAIL", "env-coach@demo.invalid")
        monkeypatch.setenv("MESO_DEMO_COACH_PASSWORD", "env-password")
        seed()
        coach = User.objects.get(email="env-coach@demo.invalid")
        assert coach.check_password("env-password")

    def test_cli_args_win_over_env_vars(self, monkeypatch):
        monkeypatch.setenv("MESO_DEMO_COACH_EMAIL", "env-coach@demo.invalid")
        seed(email="cli-coach@demo.invalid")
        assert User.objects.filter(email="cli-coach@demo.invalid").exists()
        assert not User.objects.filter(email="env-coach@demo.invalid").exists()


class TestLoadsDemoWorkspace:
    def test_loads_demo_data(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert has_demo(coach) is True

    def test_maya_gets_a_usable_known_password(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        maya = User.objects.get(email=demo_email(coach, "maya"))
        assert maya.has_usable_password()
        assert maya.check_password(DEFAULT_COACH_PASSWORD)

    def test_maya_can_authenticate_through_the_real_backend(self, client):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        maya_email = demo_email(coach, "maya")
        assert client.login(email=maya_email, password=DEFAULT_COACH_PASSWORD)

    def test_coach_can_authenticate_through_the_real_backend(self, client):
        seed()
        assert client.login(email=COACH_EMAIL, password=DEFAULT_COACH_PASSWORD)

    def test_other_demo_athletes_keep_unusable_passwords(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        devon = User.objects.get(email=demo_email(coach, "devon"))
        assert not devon.has_usable_password()


class TestMayaLoginIsNonFatalWhenMissing:
    """White-box: the helper is graceful when the demo athlete row is absent."""

    def test_returns_none_and_does_not_raise(self):
        coach = UserFactory()  # no demo loaded — Maya's row doesn't exist
        cmd = Command()
        cmd.stderr = StringIO()
        result = cmd._ensure_maya_login(coach, "some-password")
        assert result is None


class TestIdempotent:
    def test_rerun_does_not_duplicate(self):
        seed()
        seed()
        assert User.objects.filter(email=COACH_EMAIL).count() == 1
        coach = User.objects.get(email=COACH_EMAIL)
        assert CoachProfile.objects.filter(user=coach).count() == 1
        assert CoachSubscription.objects.filter(coach=coach).count() == 1
        assert has_demo(coach) is True

    def test_rerun_restores_the_known_password_if_changed(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        coach.set_password("someone-changed-it")
        coach.save(update_fields=["password"])

        seed()
        coach.refresh_from_db()
        assert coach.check_password(DEFAULT_COACH_PASSWORD)

    def test_rerun_restores_mayas_password_if_changed(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        maya = User.objects.get(email=demo_email(coach, "maya"))
        maya.set_password("someone-changed-it")
        maya.save(update_fields=["password"])

        seed()
        # The rerun reset the workspace, so Maya is a *fresh* row (same
        # deterministic email) — re-fetch rather than refresh the deleted one.
        maya = User.objects.get(email=demo_email(coach, "maya"))
        assert maya.check_password(DEFAULT_COACH_PASSWORD)

    def test_rerun_resets_prior_agent_batches(self):
        # ``load_demo`` alone would top up and keep run N-1's applied batches —
        # whose designer chat thread would then replay on camera in run N. The
        # command must reset (clear_demo → load_demo) to a pristine workspace.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        maya = User.objects.get(email=demo_email(coach, "maya"))
        plan = Plan.objects.filter(
            relationship__coach=coach, relationship__athlete=maya
        ).first()
        assert plan is not None
        AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction="a previous recording's run",
            status=AgentProposalBatch.Status.APPLIED,
        )

        seed()
        assert not AgentProposalBatch.objects.filter(coach=coach).exists()
        assert has_demo(coach) is True


class TestJsonOutput:
    def test_json_shape_and_no_password_leak(self):
        out = StringIO()
        call_command("seed_demo_recording", json=True, stdout=out)
        payload = json.loads(out.getvalue().strip())
        coach = User.objects.get(email=COACH_EMAIL)
        maya_email = demo_email(coach, "maya")
        assert payload == {"coach_email": COACH_EMAIL, "athlete_email": maya_email}
        assert DEFAULT_COACH_PASSWORD not in out.getvalue()

    def test_human_readable_output_has_no_password_either(self):
        out = StringIO()
        call_command("seed_demo_recording", stdout=out)
        assert DEFAULT_COACH_PASSWORD not in out.getvalue()
        assert COACH_EMAIL in out.getvalue()


class TestDebugGuard:
    def test_refuses_without_force(self, settings):
        settings.DEBUG = False
        with pytest.raises(CommandError):
            call_command("seed_demo_recording")
        assert not User.objects.filter(email=COACH_EMAIL).exists()

    def test_allows_with_force(self, settings):
        settings.DEBUG = False
        seed(force=True)
        assert User.objects.filter(email=COACH_EMAIL).exists()
