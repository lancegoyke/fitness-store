"""Guided demo onboarding tour — Phase 2 sandbox + Phase 3 real coach (#430).

Phase 1 (``test_demo_segments.py``) split ``load_demo`` into idempotent
per-feature segment loaders. Phase 2 drove them from an in-app guided tour
and flipped ``create_sandbox`` to an **empty start** (the tour populates the
workspace step by step instead). Phase 3 extends the same eight steps to a
real, authenticated coach: instead of loading fake demo data, the actions
guide them to add *themselves* as an athlete and program for themselves
(O5) — a distinct "self" variant of the config, derived per-request
(``tour.variant_for``) rather than stored. Covers:

- ``tour.STEPS`` data sanity (every ``url_name`` reverses, every named
  sandbox segment exists in ``meso_demo.SEGMENTS``, every step carries both
  variants);
- the ``tour.py`` helpers directly (``tour_status``/``is_active``/
  ``is_touring``/``variant_for``/``start_tour``/``set_step``/``dismiss``/
  ``complete``/``build_config``);
- ``create_sandbox``'s empty-start flip: a fresh sandbox has no demo data and
  an active tour parked at step 0;
- the roster's tour mount + embedded config JSON — present for an active
  sandbox coach or an explicitly-touring real coach, absent for a real coach
  who never started (Phase 3's stricter gate) or dismissed/completed either
  variant, and the static "Get started" card is suppressed while touring;
- the self variant's build_config resolution: ``welcome``/``designer``/
  ``agent`` steps' typed ``action``/``loaded`` gating off the coach's
  self-link, working plan, and agent allowance;
- the roster's empty-workspace tour-entry card (Phase 3) and its
  ``tour_state`` "restart" hop into a mounted tour;
- ``meso:tour_state`` (advance/back/goto clamp + persist, dismiss/complete
  persist, restart resets, anonymous -> login, AJAX vs. plain-POST shape);
- ``meso:tour_skip`` (the O6 "skip · load everything" shortcut): loads the
  full aggregate demo and marks the tour complete.
"""

import json
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso import tour
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import MesoGroup
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def _coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


# ---------------------------------------------------------------------------
# STEPS data sanity
# ---------------------------------------------------------------------------


class TestStepsSanity:
    def test_every_step_url_name_reverses(self):
        for step in tour.STEPS:
            reverse(step["url_name"])  # raises NoReverseMatch on a typo

    def test_every_named_sandbox_segment_exists_in_demo_segments(self):
        for step in tour.STEPS:
            segment = step["sandbox"].get("segment")
            if segment is not None:
                assert segment in demo.SEGMENTS

    def test_eight_steps(self):
        assert len(tour.STEPS) == 8

    def test_step_keys_are_unique(self):
        keys = [step["key"] for step in tour.STEPS]
        assert len(keys) == len(set(keys))

    def test_every_step_carries_both_variants(self):
        # Phase 3: shared key/url_name/anchor, variant-specific title/body/action.
        for step in tour.STEPS:
            assert "sandbox" in step
            assert "self" in step
            assert step["sandbox"]["title"]
            assert step["sandbox"]["body"]
            assert step["self"]["title"]
            assert step["self"]["body"]


# ---------------------------------------------------------------------------
# tour.py helpers
# ---------------------------------------------------------------------------


class TestTourStatus:
    def test_never_started_is_empty(self):
        coach = _coach()
        assert tour.tour_status(coach) == {}

    def test_no_profile_is_empty(self):
        user = UserFactory()
        assert tour.tour_status(user) == {}

    def test_reads_the_stored_state(self):
        coach = _coach()
        coach.coach_profile.tour_state = {"step": 3, "status": "active"}
        coach.coach_profile.save(update_fields=["tour_state"])
        assert tour.tour_status(coach) == {"step": 3, "status": "active"}


class TestIsActive:
    def test_never_started_is_active(self):
        coach = _coach()
        assert tour.is_active(coach) is True

    def test_in_progress_is_active(self):
        coach = _coach()
        tour.set_step(coach.coach_profile, 2)
        assert tour.is_active(coach) is True

    @pytest.mark.parametrize("status", ["dismissed", "completed"])
    def test_hidden_statuses_are_not_active(self, status):
        coach = _coach()
        coach.coach_profile.tour_state = {"step": 1, "status": status}
        coach.coach_profile.save(update_fields=["tour_state"])
        assert tour.is_active(coach) is False


class TestIsTouring:
    """The Phase 3 real-coach mount gate — stricter than ``is_active``."""

    def test_never_started_is_not_touring(self):
        # The one case that differs from is_active: a never-started `{}` is
        # "active" (not hidden) but not explicitly touring.
        coach = _coach()
        assert tour.is_touring(coach) is False

    def test_explicitly_started_is_touring(self):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        assert tour.is_touring(coach) is True

    def test_in_progress_is_touring(self):
        coach = _coach()
        tour.set_step(coach.coach_profile, 2)
        assert tour.is_touring(coach) is True

    @pytest.mark.parametrize("status", ["dismissed", "completed"])
    def test_hidden_statuses_are_not_touring(self, status):
        coach = _coach()
        coach.coach_profile.tour_state = {"step": 1, "status": status}
        coach.coach_profile.save(update_fields=["tour_state"])
        assert tour.is_touring(coach) is False


class TestStartSetDismissComplete:
    def test_start_tour_resets_to_step_zero_active(self):
        coach = _coach()
        profile = coach.coach_profile
        profile.tour_state = {"step": 5, "status": "dismissed"}
        profile.save(update_fields=["tour_state"])

        tour.start_tour(profile)

        profile.refresh_from_db()
        assert profile.tour_state == {"step": 0, "status": "active"}

    def test_set_step_clamps_into_range(self):
        coach = _coach()
        profile = coach.coach_profile

        tour.set_step(profile, 99)
        profile.refresh_from_db()
        assert profile.tour_state == {
            "step": len(tour.STEPS) - 1,
            "status": "active",
        }

        tour.set_step(profile, -3)
        profile.refresh_from_db()
        assert profile.tour_state == {"step": 0, "status": "active"}

    def test_dismiss_preserves_the_step(self):
        coach = _coach()
        profile = coach.coach_profile
        tour.set_step(profile, 4)

        tour.dismiss(profile)

        profile.refresh_from_db()
        assert profile.tour_state == {"step": 4, "status": "dismissed"}

    def test_complete_parks_on_the_last_step(self):
        coach = _coach()
        profile = coach.coach_profile
        tour.set_step(profile, 1)

        tour.complete(profile)

        profile.refresh_from_db()
        assert profile.tour_state == {
            "step": len(tour.STEPS) - 1,
            "status": "completed",
        }


class TestBuildConfig:
    """Exercises the sandbox variant explicitly — the Phase 2 contract, untouched.

    ``build_config`` now takes a required ``variant`` (Phase 3) instead of
    inferring it, so these calls pass ``"sandbox"`` explicitly; the coach
    fixture (``_coach()``, no ``SandboxSession``) predates the variant split
    and was always exercising the segment-based behavior these tests assert.
    """

    def test_none_without_a_coach_profile(self):
        user = UserFactory()
        assert tour.build_config(user, "sandbox") is None

    def test_carries_all_steps_with_resolved_urls(self):
        coach = _coach()
        config = tour.build_config(coach, "sandbox")
        assert len(config["steps"]) == len(tour.STEPS)
        for step, spec in zip(config["steps"], tour.STEPS):
            assert step["key"] == spec["key"]
            assert step["url"] == reverse(spec["url_name"])

    def test_loaded_flags_start_false_and_flip_with_the_segment(self):
        coach = _coach()
        config = tour.build_config(coach, "sandbox")
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["loaded"] is False

        demo.load_athletes(coach)

        config = tour.build_config(coach, "sandbox")
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["loaded"] is True

    def test_steps_with_no_segment_have_no_loaded_flag(self):
        coach = _coach()
        config = tour.build_config(coach, "sandbox")
        profile_step = next(s for s in config["steps"] if s["key"] == "profile")
        assert profile_step["loaded"] is None

    def test_endpoints_and_current_progress(self):
        coach = _coach()
        tour.set_step(coach.coach_profile, 2)

        config = tour.build_config(coach, "sandbox")

        assert config["step"] == 2
        assert config["status"] == "active"
        assert config["state_url"] == reverse("meso:tour_state")
        assert config["skip_url"] == reverse("meso:tour_skip")
        assert config["demo_load_url"] == reverse("meso:demo_load")
        assert config["signup_url"] == reverse("meso:sandbox_signup")

    def test_sandbox_steps_never_carry_a_generic_action(self):
        # The additive Phase 3 `action` field must stay null for every sandbox
        # step — the driver's segment/signup_gate branches are untouched.
        coach = _coach()
        config = tour.build_config(coach, "sandbox")
        for step in config["steps"]:
            assert step["action"] is None

    def test_config_carries_the_variant(self):
        # #441 P1-1: the driver needs to know which audience it's rendering for
        # so it can hide the sandbox-only "load everything" skip in the self
        # variant (a real coach must never load fake demo athletes).
        coach = _coach()
        assert tour.build_config(coach, "sandbox")["variant"] == "sandbox"
        assert tour.build_config(coach, "self")["variant"] == "self"


class TestBuildConfigGotoReady:
    """#441 P1-3: "Take me there" must not point at a page that'll bounce.

    designer/deliver/results redirect back to the roster (an uncorrelated
    flash + an infinite retry loop) when the coach has no plan / no logged
    session yet. ``goto_ready`` mirrors the view's own redirect predicate — and
    those view helpers are identity-blind, so the same flag works for both
    variants. Steps that target the roster (welcome/profile/groups/finish)
    never dead-end, so they're always ready.
    """

    def _goto(self, config, key):
        return next(s for s in config["steps"] if s["key"] == key)["goto_ready"]

    def test_sandbox_fresh_gates_the_data_dependent_gotos(self):
        user = sandbox.create_sandbox()
        config = tour.build_config(user, "sandbox")
        assert self._goto(config, "designer") is False
        assert self._goto(config, "deliver") is False
        assert self._goto(config, "results") is False
        assert self._goto(config, "agent") is False

    def test_sandbox_full_demo_opens_the_gotos(self):
        user = sandbox.create_sandbox()
        demo.load_demo(user)
        config = tour.build_config(user, "sandbox")
        assert self._goto(config, "designer") is True
        assert self._goto(config, "deliver") is True
        assert self._goto(config, "results") is True
        assert self._goto(config, "agent") is True

    def test_roster_targeted_steps_are_always_ready(self):
        user = sandbox.create_sandbox()
        config = tour.build_config(user, "sandbox")
        for key in ("welcome", "profile", "groups", "finish"):
            assert self._goto(config, key) is True

    def test_self_plan_opens_designer_deliver_agent_but_not_results(self):
        coach = _coach()
        link = CoachAthlete.add_self(coach)
        link.create_plan()
        config = tour.build_config(coach, "self")
        assert self._goto(config, "designer") is True
        assert self._goto(config, "deliver") is True
        assert self._goto(config, "agent") is True
        # No logged session yet → results still bounces.
        assert self._goto(config, "results") is False


class TestVariantFor:
    def test_sandbox_coach_is_sandbox(self):
        user = sandbox.create_sandbox()
        assert tour.variant_for(user) == "sandbox"

    def test_real_coach_is_self(self):
        coach = _coach()
        assert tour.variant_for(coach) == "self"

    def test_anonymous_is_self(self):
        # Never actually rendered (no CoachProfile, no page reaches this), but
        # variant_for shouldn't itself blow up on a non-sandbox user.
        user = UserFactory()
        assert tour.variant_for(user) == "self"


class TestBuildConfigSelfVariant:
    """The self-coaching variant's data-dependent resolution (Phase 3, O5)."""

    def test_welcome_offers_roster_add_self_and_is_unloaded_at_first(self):
        coach = _coach()
        config = tour.build_config(coach, "self")
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["segment"] is None
        assert welcome["loaded"] is False
        assert welcome["action"] == {
            "url": reverse("meso:roster_add_self"),
            "label": "Add yourself as your first athlete",
            "fields": {},
        }

    def test_welcome_loaded_flips_once_the_self_link_exists(self):
        coach = _coach()
        CoachAthlete.add_self(coach)

        config = tour.build_config(coach, "self")

        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["loaded"] is True
        # Still offered (disabled "Done" state is the driver's job, not ours).
        assert welcome["action"] is not None

    def test_designer_has_no_action_without_a_self_link(self):
        coach = _coach()
        config = tour.build_config(coach, "self")
        designer = next(s for s in config["steps"] if s["key"] == "designer")
        assert designer["action"] is None
        assert designer["loaded"] is False
        assert "welcome step" in designer["body"]

    def test_designer_offers_plan_create_once_the_self_link_exists(self):
        coach = _coach()
        CoachAthlete.add_self(coach)

        config = tour.build_config(coach, "self")

        designer = next(s for s in config["steps"] if s["key"] == "designer")
        assert designer["loaded"] is False
        assert designer["action"] == {
            "url": reverse("meso:plan_create", args=[coach.pk]),
            "label": "Start a program for yourself",
            "fields": {},
        }

    def test_designer_loaded_once_the_self_link_has_a_working_plan(self):
        coach = _coach()
        link = CoachAthlete.add_self(coach)
        link.create_plan()

        config = tour.build_config(coach, "self")

        designer = next(s for s in config["steps"] if s["key"] == "designer")
        assert designer["loaded"] is True
        assert designer["action"] is not None

    def test_agent_has_no_action_without_a_self_link(self):
        coach = _coach()
        config = tour.build_config(coach, "self")
        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None
        assert agent["signup_gate"] is False

    def test_agent_offers_the_draft_action_for_a_fresh_coach_with_a_self_link(self):
        # A fresh coach has no CoachSubscription row → billing_status FREE,
        # and FREE_AGENT_ALLOWANCE (5) > 0 runs used → can_use_agent is True.
        coach = _coach()
        CoachAthlete.add_self(coach)

        config = tour.build_config(coach, "self")

        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] == {
            "url": reverse("meso:plan_create", args=[coach.pk]),
            "label": "Draft next block with AI",
            "fields": {"draft": "agent"},
        }

    def test_agent_has_no_action_once_the_free_allowance_is_exhausted(self):
        coach = _coach()
        CoachAthlete.add_self(coach)
        # An unrelated plan/relationship just to attribute the batches to this
        # coach — the self-link's own plan stays empty throughout.
        other_plan = PlanFactory(relationship=CoachAthleteFactory(coach=coach))
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(plan=other_plan, coach=coach)

        config = tour.build_config(coach, "self")

        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None

    def test_agent_has_no_action_once_a_working_plan_already_exists(self):
        coach = _coach()
        link = CoachAthlete.add_self(coach)
        link.create_plan()

        config = tour.build_config(coach, "self")

        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None

    def test_agent_locked_copy_without_a_self_link_points_at_welcome(self):
        # #441 P1-5: the locked copy must name the actual blocker. No self-link
        # yet → do the welcome step first (mirrors the designer step).
        coach = _coach()
        config = tour.build_config(coach, "self")
        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None
        assert "welcome step" in agent["body"]

    def test_agent_locked_copy_when_a_plan_exists_points_at_the_designer(self):
        # #441 P1-5: step 3 already created this plan, so the old copy ("start a
        # free trial ... once you have an active program") told the coach to go
        # acquire what they already have. Point them at the block instead.
        coach = _coach()
        link = CoachAthlete.add_self(coach)
        link.create_plan()
        config = tour.build_config(coach, "self")
        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None
        assert "Designer" in agent["body"]
        assert "trial" not in agent["body"].lower()

    def test_agent_locked_copy_when_out_of_allowance_mentions_a_trial(self):
        # #441 P1-5: a self-link + no plan + exhausted free allowance is the one
        # case where "start a trial" is the right advice.
        coach = _coach()
        CoachAthlete.add_self(coach)  # link, but its own plan stays empty
        other_plan = PlanFactory(relationship=CoachAthleteFactory(coach=coach))
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(plan=other_plan, coach=coach)
        config = tour.build_config(coach, "self")
        agent = next(s for s in config["steps"] if s["key"] == "agent")
        assert agent["action"] is None
        assert "trial" in agent["body"].lower()

    def test_finish_has_no_signup_gate_and_anchors_the_invite_control(self):
        coach = _coach()
        config = tour.build_config(coach, "self")
        finish = next(s for s in config["steps"] if s["key"] == "finish")
        assert finish["signup_gate"] is False
        assert finish["anchor"] == "roster-invite"
        assert "Invite your first real athlete" in finish["body"]

    @pytest.mark.parametrize("key", ["profile", "deliver", "results", "groups"])
    def test_action_less_self_steps_offer_no_action_or_segment(self, key):
        # These self steps have no typed action button and never carry a sandbox
        # ``segment`` — the coach uses the real spotlighted control. (#441 P3-5:
        # deliver/results/groups now DO carry a data-derived ``loaded`` flag; see
        # ``TestSelfActionGoalLoadedCopy``. Only ``profile`` stays loaded-less.)
        coach = _coach()
        config = tour.build_config(coach, "self")
        step = next(s for s in config["steps"] if s["key"] == key)
        assert step["action"] is None
        assert step["segment"] is None

    def test_profile_self_step_has_no_loaded_flag(self):
        coach = _coach()
        config = tour.build_config(coach, "self")
        profile_step = next(s for s in config["steps"] if s["key"] == "profile")
        assert profile_step["loaded"] is None

    def test_posting_roster_add_self_flips_welcome_and_unlocks_designer(self, client):
        # An end-to-end version of the two tests above, through the real view.
        coach = _coach()
        client.force_login(coach)

        client.post(reverse("meso:roster_add_self"))

        config = tour.build_config(coach, "self")
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        designer = next(s for s in config["steps"] if s["key"] == "designer")
        assert welcome["loaded"] is True
        assert designer["action"] is not None
        assert designer["action"]["url"] == reverse("meso:plan_create", args=[coach.pk])


# ---------------------------------------------------------------------------
# create_sandbox's empty-start flip (the visible Phase 2 change)
# ---------------------------------------------------------------------------


class TestSandboxEmptyStart:
    def test_fresh_sandbox_has_no_demo_data(self):
        user = sandbox.create_sandbox()
        assert demo.has_demo(user) is False

    def test_fresh_sandbox_tour_is_active_at_step_zero(self):
        user = sandbox.create_sandbox()
        assert CoachProfile.objects.get(user=user).tour_state == {
            "step": 0,
            "status": "active",
        }


# ---------------------------------------------------------------------------
# Roster: the tour mount + embedded config JSON
# ---------------------------------------------------------------------------


class TestRosterTourMount:
    def test_active_sandbox_coach_sees_the_tour_mount_and_config(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body

    def test_real_coach_never_sees_the_tour(self, client):
        # A real coach who has never touched the tour: `{}` — not the literal
        # `"active"` `is_touring` requires — so the tour never self-mounts
        # (Phase 3's stricter real-coach gate; they opt in via the entry card).
        coach = _coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body
        assert 'id="meso-tour-config"' not in body

    def test_real_coach_with_an_explicitly_active_tour_sees_the_mount(self, client):
        # Phase 3: a real coach who opted in (the entry card's "restart" POST,
        # or here directly via start_tour) does get the mount.
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body

    def test_real_coachs_dismissed_tour_does_not_mount(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.dismiss(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body

    def test_real_coachs_completed_tour_does_not_mount_and_original_card_returns(
        self, client
    ):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.complete(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body
        assert "Welcome to your coaching workspace" in body
        assert "Start the guided tour" not in body

    def test_dismissed_sandbox_coach_does_not_see_the_tour(self, client):
        user = sandbox.create_sandbox()
        tour.dismiss(CoachProfile.objects.get(user=user))
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body

    def test_completed_sandbox_coach_does_not_see_the_tour(self, client):
        user = sandbox.create_sandbox()
        tour.complete(CoachProfile.objects.get(user=user))
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body

    def test_the_empty_state_get_started_card_is_suppressed_while_touring(self, client):
        """The tour's own welcome step replaces roster.html's static card."""
        user = sandbox.create_sandbox()
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" not in body

    def test_a_real_coachs_empty_workspace_still_shows_the_static_card(self, client):
        """Zero change for a real coach (Phase 2 is sandbox-only)."""
        coach = _coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" in body

    def test_config_json_embeds_the_current_step_and_all_steps(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()
        start = body.index('id="meso-tour-config"')
        script_start = body.index(">", start) + 1
        script_end = body.index("</script>", script_start)
        config = json.loads(body[script_start:script_end])

        assert config["step"] == 0
        assert config["status"] == "active"
        assert len(config["steps"]) == 8

    def test_a_touring_real_coachs_config_uses_the_self_variant(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        start = body.index('id="meso-tour-config"')
        script_start = body.index(">", start) + 1
        script_end = body.index("</script>", script_start)
        config = json.loads(body[script_start:script_end])

        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["segment"] is None
        assert welcome["action"]["url"] == reverse("meso:roster_add_self")


# ---------------------------------------------------------------------------
# Roster: the empty-workspace tour-entry card (Phase 3) — replaces the static
# Get-started card's CTA for a real coach who hasn't dismissed/completed the
# tour, while leaving a dismissed/completed tour's card exactly as before.
# ---------------------------------------------------------------------------


class TestTourEntryCard:
    def test_fresh_real_coach_sees_the_tour_entry_card(self, client):
        coach = _coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" in body
        assert "Start the guided tour" in body

    def test_dismissed_tour_shows_the_original_card_without_the_entry(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.dismiss(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" in body
        assert "Start the guided tour" not in body

    def test_completed_tour_shows_the_original_card_without_the_entry(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.complete(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" in body
        assert "Start the guided tour" not in body

    def test_a_non_empty_workspace_never_shows_the_get_started_card(self, client):
        coach = _coach()
        CoachAthleteFactory(coach=coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Welcome to your coaching workspace" not in body
        assert "Start the guided tour" not in body

    def test_posting_restart_from_the_entry_card_mounts_the_tour(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:tour_state"), {"action": "restart"}, follow=True
        )

        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body
        assert CoachProfile.objects.get(user=coach).tour_state == {
            "step": 0,
            "status": "active",
        }


# ---------------------------------------------------------------------------
# The tour mount renders on every coach page, including designer.html — the
# one standalone document that doesn't extend _meso_base.html and wires the
# include in separately (see designer.html's own comment).
# ---------------------------------------------------------------------------


class TestTourRendersAcrossCoachPages:
    def test_designer(self, client):
        user = sandbox.create_sandbox()
        demo.load_program(user)
        client.force_login(user)

        body = client.get(reverse("meso:designer"), follow=True).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body

    def test_deliver(self, client):
        user = sandbox.create_sandbox()
        demo.load_delivery(user)
        client.force_login(user)

        body = client.get(reverse("meso:deliver"), follow=True).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body

    def test_results(self, client):
        user = sandbox.create_sandbox()
        demo.load_log(user)
        client.force_login(user)

        body = client.get(reverse("meso:results"), follow=True).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body

    def test_athlete_profile(self, client):
        user = sandbox.create_sandbox()
        demo.load_athletes(user)
        athlete, _link = demo._demo_athlete_and_link(user, "maya")
        client.force_login(user)

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": athlete.pk})
        ).content.decode()

        assert 'id="meso-tour"' in body
        assert 'id="meso-tour-config"' in body


# ---------------------------------------------------------------------------
# meso:tour_state
# ---------------------------------------------------------------------------


class TestTourStateEndpoint:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.post(reverse("meso:tour_state"), {"action": "advance"})
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url

    def test_advance_persists_and_returns_json_for_ajax(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "advance"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.status_code == 200
        assert resp.json() == {"step": 1, "status": "active"}
        assert CoachProfile.objects.get(user=user).tour_state == {
            "step": 1,
            "status": "active",
        }

    def test_advance_clamps_at_the_last_step(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, len(tour.STEPS) - 1)
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "advance"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.json()["step"] == len(tour.STEPS) - 1

    def test_back_clamps_at_zero(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "back"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.json()["step"] == 0

    def test_goto_jumps_to_a_step_and_clamps_an_out_of_range_one(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "goto", "step": "4"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert resp.json()["step"] == 4

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "goto", "step": "999"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert resp.json()["step"] == len(tour.STEPS) - 1

    def test_dismiss_persists(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "dismiss"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.json()["status"] == "dismissed"
        assert CoachProfile.objects.get(user=user).tour_state["status"] == "dismissed"

    def test_complete_persists(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "complete"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.json() == {"step": len(tour.STEPS) - 1, "status": "completed"}

    def test_restart_resets_to_step_zero_active(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.complete(profile)
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "restart"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.json() == {"step": 0, "status": "active"}

    def test_unknown_action_400s(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(
            reverse("meso:tour_state"),
            {"action": "bogus"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert resp.status_code == 400

    def test_non_ajax_post_redirects_to_roster(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(reverse("meso:tour_state"), {"action": "advance"})

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    def test_ensures_a_coach_profile_for_a_bare_user(self, client):
        user = UserFactory()  # no CoachProfile yet
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "advance"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert CoachProfile.objects.filter(user=user).exists()


# ---------------------------------------------------------------------------
# meso:tour_skip — the O6 "skip · load everything" shortcut
# ---------------------------------------------------------------------------


class TestTourSkipEndpoint:
    def test_loads_the_full_demo_and_completes_the_tour(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(reverse("meso:tour_skip"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(user) is True
        assert demo.has_program(user) is True
        assert demo.has_group(user) is True
        assert CoachProfile.objects.get(user=user).tour_state["status"] == "completed"

    def test_five_athletes_loaded(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        client.post(reverse("meso:tour_skip"))

        assert len(list(demo._demo_athletes(user))) == 5

    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.post(reverse("meso:tour_skip"))
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url

    def test_flashes_the_demo_loaded_message(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        resp = client.post(reverse("meso:tour_skip"), follow=True)

        assert "demo data loaded" in resp.content.decode().lower()

    def test_ensures_a_coach_profile(self, client):
        user = UserFactory()
        client.force_login(user)

        client.post(reverse("meso:tour_skip"))

        assert CoachProfile.objects.filter(user=user).exists()

    def test_self_variant_completes_without_loading_demo(self, client):
        # #441 P1-1: a real (self-variant) coach must never get the 5 fake demo
        # athletes on their live roster from the skip shortcut (O5). The skip
        # form is hidden in the self variant, but the endpoint guards it too.
        coach = _coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:tour_skip"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert demo.has_demo(coach) is False
        assert CoachProfile.objects.get(user=coach).tour_state["status"] == "completed"


# ---------------------------------------------------------------------------
# #441 P1-2: step 2 ("click into your profile") completion detection
# ---------------------------------------------------------------------------


class TestProfileStepAnchor:
    def test_sandbox_profile_keeps_the_whole_list_spotlight(self):
        # Maya (the athlete the copy names) can't be identified row-by-row at
        # this step — the roster sorts by name so the first row is Devon, and
        # her demo program only loads later — so the sandbox spotlights the
        # whole list rather than mis-targeting a row.
        coach = _coach()
        config = tour.build_config(coach, "sandbox")
        profile = next(s for s in config["steps"] if s["key"] == "profile")
        assert profile["anchor"] == "roster-athlete-rows"

    def test_self_profile_anchors_the_coachs_own_row(self):
        # The self variant has exactly one row (the coach) — spotlight it.
        coach = _coach()
        config = tour.build_config(coach, "self")
        profile = next(s for s in config["steps"] if s["key"] == "profile")
        assert profile["anchor"] == "roster-athlete-row-first"

    def test_roster_marks_the_self_row_for_the_spotlight(self, client):
        coach = _coach()
        CoachAthlete.add_self(coach)
        tour.start_tour(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'data-tour="roster-athlete-row-first"' in body


class TestAdvanceIfOnStep:
    """``tour.advance_if_on_step`` — page-visit completion for the profile step.

    A no-op unless the coach is actively touring AND parked exactly on the
    named step; then it advances one step forward.
    """

    def test_advances_a_touring_coach_parked_on_the_step(self):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.set_step(coach.coach_profile, 1)  # the profile step

        assert tour.advance_if_on_step(coach, "profile") is True
        assert tour.tour_status(coach)["step"] == 2

    def test_noop_when_parked_on_a_different_step(self):
        coach = _coach()
        tour.start_tour(coach.coach_profile)  # step 0 (welcome)

        assert tour.advance_if_on_step(coach, "profile") is False
        assert tour.tour_status(coach)["step"] == 0

    def test_noop_when_not_touring(self):
        coach = _coach()  # never started → {} → not active

        assert tour.advance_if_on_step(coach, "profile") is False

    def test_noop_when_dismissed(self):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.set_step(coach.coach_profile, 1)
        tour.dismiss(coach.coach_profile)

        assert tour.advance_if_on_step(coach, "profile") is False

    def test_noop_without_a_coach_profile(self):
        # Defensive: an athlete with no CoachProfile visiting a profile page.
        user = UserFactory()
        assert tour.advance_if_on_step(user, "profile") is False


class TestProfileVisitAdvance:
    """The auto-advance wired into ``AthleteProfileView`` (#441 P1-2)."""

    def test_visiting_an_athlete_profile_advances_the_profile_step(self, client):
        coach = _coach()
        CoachAthlete.add_self(coach)  # the coach's own athlete profile
        tour.start_tour(coach.coach_profile)
        tour.set_step(coach.coach_profile, 1)  # the profile step
        client.force_login(coach)

        client.get(reverse("meso:athlete", args=[coach.pk]))

        assert tour.tour_status(coach)["step"] == 2

    def test_visiting_a_profile_off_the_profile_step_does_not_advance(self, client):
        coach = _coach()
        CoachAthlete.add_self(coach)
        tour.start_tour(coach.coach_profile)  # step 0 (welcome)
        client.force_login(coach)

        client.get(reverse("meso:athlete", args=[coach.pk]))

        assert tour.tour_status(coach)["step"] == 0


# ---------------------------------------------------------------------------
# #441 P2: state-aware copy — steps' bodies react to what the coach has
# actually done (data-derived from the same predicates the tour already
# computes), rather than always reading as an unstarted call to action.
# ---------------------------------------------------------------------------


def _step(config, key):
    return next(s for s in config["steps"] if s["key"] == key)


class TestAdaptiveDoneCopySandbox:
    """A loaded sandbox segment swaps its step body to a "done" variant (P2-1)."""

    def test_welcome_body_adapts_to_loaded_state(self):
        coach = _coach()
        welcome = _step(tour.build_config(coach, "sandbox"), "welcome")
        assert "Add 5 sample athletes" in welcome["body"]

        demo.load_athletes(coach)

        welcome = _step(tour.build_config(coach, "sandbox"), "welcome")
        assert "are here now" in welcome["body"]
        assert "Add 5 sample athletes" not in welcome["body"]

    def test_designer_body_shows_done_after_load_program(self):
        coach = _coach()
        demo.load_program(coach)
        designer = _step(tour.build_config(coach, "sandbox"), "designer")
        assert "sample program is loaded" in designer["body"]

    def test_deliver_body_shows_done_after_load_delivery(self):
        coach = _coach()
        demo.load_delivery(coach)
        deliver = _step(tour.build_config(coach, "sandbox"), "deliver")
        assert "Delivered" in deliver["body"]

    def test_results_body_shows_done_after_load_log(self):
        coach = _coach()
        demo.load_log(coach)
        results = _step(tour.build_config(coach, "sandbox"), "results")
        assert "is logged" in results["body"]

    def test_groups_body_shows_done_after_load_group(self):
        coach = _coach()
        demo.load_group(coach)
        groups = _step(tour.build_config(coach, "sandbox"), "groups")
        assert "Sample group added" in groups["body"]


class TestAdaptiveDoneCopySelf:
    """The self variant's welcome/designer bodies flip to "done" too (P2-1)."""

    def test_welcome_body_adapts_to_loaded_state(self):
        coach = _coach()
        welcome = _step(tour.build_config(coach, "self"), "welcome")
        assert "Add yourself as your first athlete" in welcome["body"]

        CoachAthlete.add_self(coach)

        welcome = _step(tour.build_config(coach, "self"), "welcome")
        assert "on it now" in welcome["body"]
        assert "Add yourself as your first athlete" not in welcome["body"]

    def test_designer_body_shows_done_after_plan(self):
        coach = _coach()
        link = CoachAthlete.add_self(coach)
        link.create_plan()
        designer = _step(tour.build_config(coach, "self"), "designer")
        assert "program is started" in designer["body"]


class TestProfilePrerequisiteCopy:
    """The profile step's body is gated on its prerequisite existing (P2-2)."""

    def test_sandbox_profile_locked_without_athletes(self):
        coach = _coach()
        profile = _step(tour.build_config(coach, "sandbox"), "profile")
        assert "Add the sample athletes first" in profile["body"]
        assert "Click into Maya" not in profile["body"]
        assert profile["loaded"] is None

    def test_sandbox_profile_unlocked_after_load_athletes(self):
        coach = _coach()
        demo.load_athletes(coach)
        profile = _step(tour.build_config(coach, "sandbox"), "profile")
        assert "Click into Maya" in profile["body"]

    def test_self_profile_locked_without_a_self_link(self):
        coach = _coach()
        profile = _step(tour.build_config(coach, "self"), "profile")
        assert "Add yourself as an athlete first" in profile["body"]
        assert "Click into your own profile" not in profile["body"]
        assert profile["action"] is None
        assert profile["loaded"] is None

    def test_self_profile_unlocked_after_add_self(self):
        coach = _coach()
        CoachAthlete.add_self(coach)
        profile = _step(tour.build_config(coach, "self"), "profile")
        assert "Click into your own profile" in profile["body"]


class TestFinishCopyBranchesOnHasDemo:
    """The sandbox finish's copy branches on whether there's demo data (P2-4).

    It only promises "remove the demo data" when there's some to remove, and
    never claims a tour "start over".
    """

    def test_no_demo_copy_omits_the_removal_promise(self):
        coach = _coach()
        finish = _step(tour.build_config(coach, "sandbox"), "finish")
        assert "Create a free account" in finish["body"]
        assert "sample data" not in finish["body"]
        assert "remove" not in finish["body"].lower()
        assert "start over" not in finish["body"].lower()

    def test_has_demo_copy_offers_removal(self):
        coach = _coach()
        demo.load_demo(coach)
        finish = _step(tour.build_config(coach, "sandbox"), "finish")
        assert "remove it any time" in finish["body"]
        assert "start over" not in finish["body"].lower()


class TestDemoBannerDecoupling:
    """The demo-removal banner and the mounted tour are mutually exclusive.

    P2-5a: during an active tour with demo loaded neither the banner nor the
    Get-started card renders.
    """

    def test_banner_suppressed_while_touring(self, client):
        user = sandbox.create_sandbox()
        demo.load_athletes(user)
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Demo workspace" not in body
        assert reverse("meso:demo_clear") not in body

    def test_banner_shows_for_a_non_touring_real_coach(self, client):
        coach = _coach()
        demo.load_demo(coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Demo workspace" in body


class TestDemoClearResetsTour:
    """Clearing the demo mid-tour restarts the tour at step 0 (P2-5b).

    Removing the demo data the tour was walking you through would otherwise
    park the step index on a now-empty workspace, so an actively-touring coach
    is reset; a dismissed/completed tour is left alone.
    """

    def test_resets_a_mid_flight_tour_to_step_zero(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 3)
        demo.load_athletes(user)
        client.force_login(user)

        client.post(reverse("meso:demo_clear"))

        assert tour.tour_status(user) == {"step": 0, "status": "active"}

    def test_leaves_a_non_touring_tour_alone(self, client):
        coach = _coach()
        tour.complete(coach.coach_profile)
        demo.load_demo(coach)
        client.force_login(coach)

        client.post(reverse("meso:demo_clear"))

        assert tour.tour_status(coach)["status"] == "completed"


class TestSandboxFallbackCardCopy:
    """The Get-started card's fallback copy is sandbox-aware (P2-6).

    It never dangles a real-invite offer at a sandbox coach, who can't invite
    real athletes; the real-coach copy is unchanged.
    """

    def test_sandbox_hides_the_invite_copy(self, client):
        user = sandbox.create_sandbox()
        tour.dismiss(CoachProfile.objects.get(user=user))
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "inviting real athletes is off in the demo" in body
        assert "invite your first athlete below" not in body

    def test_real_coach_keeps_the_invite_copy(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.dismiss(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "invite your first athlete below" in body


class TestDurableRestartAffordance:
    """An always-available tour restart lives in the sidebar (P2-3).

    It appears once the empty-state entry card is gone (data added, or the tour
    dismissed/completed), and is hidden while the tour is mounted or on the
    fresh first run. Its "Restart" label is distinct from the entry card's
    "Start" button so the two never collide in body assertions.
    """

    def test_present_after_completing_the_tour(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)
        tour.complete(coach.coach_profile)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Restart the guided tour" in body
        assert reverse("meso:tour_state") in body
        assert 'value="restart"' in body

    def test_survives_having_workspace_data(self, client):
        coach = _coach()
        CoachAthleteFactory(coach=coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Restart the guided tour" in body

    def test_hidden_while_the_tour_is_mounted(self, client):
        user = sandbox.create_sandbox()
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Restart the guided tour" not in body

    def test_absent_on_the_fresh_empty_first_run(self, client):
        coach = _coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Start the guided tour" in body
        assert "Restart the guided tour" not in body

    def test_restored_for_a_dismissed_sandbox_coach(self, client):
        user = sandbox.create_sandbox()
        tour.dismiss(CoachProfile.objects.get(user=user))
        client.force_login(user)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Restart the guided tour" in body


# ---------------------------------------------------------------------------
# #441 P3-5: action-completion auto-advance — the tour reacts to what the coach
# actually did. Part A fills the self-variant ``loaded`` predicate gaps
# (deliver/results/groups) + their done-copy; part B wires
# ``advance_if_on_step`` into every action-completion site so each action-goal
# step advances the moment its data lands, not on a later Next click. See
# ``docs/meso/tour-auto-advance-model.md``.
# ---------------------------------------------------------------------------


def _self_plan(coach, *, delivered=False, current=True):
    """A self-link + individual plan with one week (session + prescription).

    Mirrors ``test_deliver``/``test_athlete_logging``'s seed graph, but on the
    coach's own ``is_self`` link (coach == athlete) so the self-variant
    deliver/log flow runs end to end. ``delivered`` stamps the week (a loggable,
    delivered session); ``current`` flags it live (a deliverable week).
    """
    link = CoachAthlete.add_self(coach)
    plan = PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=1,
        is_current=current,
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name="Lower")
    presc(session, name="Box Squat", sets="3", reps="6", load="70", rpe="7")
    return SimpleNamespace(link=link, plan=plan, meso=meso, week=week, session=session)


class TestSelfLoadedPredicates:
    """The self-variant ``loaded`` predicates mirror the sandbox ``demo.has_*`` (P3-5-A).

    Self-scoped (an ``is_self`` link's individual plan / ``athlete == coach`` /
    a non-demo group), so a self-coaching coach's deliver/results/groups steps
    finally get a real completion signal — the gap the auto-advance doc named.
    """

    def test_has_delivery_false_before_true_after(self):
        coach = _coach()
        assert tour._self_has_delivery(coach) is False

        _self_plan(coach, delivered=True)
        assert tour._self_has_delivery(coach) is True

    def test_has_delivery_false_for_an_undelivered_self_week(self):
        coach = _coach()
        _self_plan(coach)  # a current week, not yet delivered
        assert tour._self_has_delivery(coach) is False

    def test_has_log_false_before_true_after(self):
        coach = _coach()
        assert tour._self_has_log(coach) is False

        s = _self_plan(coach, delivered=True)
        # A pending "save progress" log doesn't count — only a done one does.
        SessionLog.objects.create(session=s.session, athlete=coach)
        assert tour._self_has_log(coach) is False

        SessionLog.objects.filter(session=s.session, athlete=coach).update(
            status=SessionLog.Status.DONE
        )
        assert tour._self_has_log(coach) is True

    def test_has_group_false_before_true_after(self):
        coach = _coach()
        assert tour._self_has_group(coach) is False

        MesoGroup.create_for_coach(coach, name="Squad")
        assert tour._self_has_group(coach) is True

    def test_has_group_ignores_demo_groups(self):
        coach = _coach()
        demo.load_group(coach)  # an ``is_demo`` group, not a real one
        assert tour._self_has_group(coach) is False


class TestSelfActionGoalLoadedCopy:
    """deliver/results/groups self steps gain a ``loaded`` flag + done-copy (P3-5-A).

    Before completion ``loaded`` is ``False`` and the body reads as a call to
    action; once the data exists ``loaded`` flips ``True`` and the body swaps to
    a "done" variant. These steps have no action button (the coach uses the real
    spotlighted control), so ``action`` stays ``None`` throughout.
    """

    def test_deliver_loaded_flips_and_body_switches_to_done(self):
        coach = _coach()
        deliver = _step(tour.build_config(coach, "self"), "deliver")
        assert deliver["loaded"] is False
        assert deliver["action"] is None
        assert "Push the current week" in deliver["body"]

        _self_plan(coach, delivered=True)

        deliver = _step(tour.build_config(coach, "self"), "deliver")
        assert deliver["loaded"] is True
        assert deliver["action"] is None
        assert "Delivered" in deliver["body"]

    def test_results_loaded_flips_and_body_switches_to_done(self):
        coach = _coach()
        results = _step(tour.build_config(coach, "self"), "results")
        assert results["loaded"] is False
        assert "Log your own sets" in results["body"]

        s = _self_plan(coach, delivered=True)
        SessionLog.objects.create(
            session=s.session, athlete=coach, status=SessionLog.Status.DONE
        )

        results = _step(tour.build_config(coach, "self"), "results")
        assert results["loaded"] is True
        assert "is logged" in results["body"]

    def test_results_pending_log_does_not_count_as_done(self):
        # A "save progress" (pending) log — or a done log on another coach's
        # plan — must not mark the self results step complete (Codex #441 P3-5).
        coach = _coach()
        s = _self_plan(coach, delivered=True)
        SessionLog.objects.create(
            session=s.session, athlete=coach, status=SessionLog.Status.PENDING
        )

        results = _step(tour.build_config(coach, "self"), "results")
        assert results["loaded"] is False
        assert "Log your own sets" in results["body"]

    def test_groups_loaded_flips_and_body_switches_to_done(self):
        coach = _coach()
        groups = _step(tour.build_config(coach, "self"), "groups")
        assert groups["loaded"] is False
        assert "Skip this one" in groups["body"]

        MesoGroup.create_for_coach(coach, name="Squad")

        groups = _step(tour.build_config(coach, "self"), "groups")
        assert groups["loaded"] is True
        assert "Your group exists" in groups["body"]


class TestActionSiteAutoAdvance:
    """Each action-goal step auto-advances at its action-completion site (P3-5-B).

    The generalization of the profile visit-advance: instead of waiting for a
    Next click, the tour advances the moment the step's data-producing action
    lands — server-side, from the same POST handler that records the opt-in.
    Gated by ``advance_if_on_step``'s parked-step check, so an action off its
    step (or by a non-touring coach) never advances.
    """

    # -- self variant -----------------------------------------------------

    def test_self_welcome_advances_on_roster_add_self(self, client):
        coach = _coach()
        tour.start_tour(coach.coach_profile)  # step 0 (welcome)
        client.force_login(coach)

        client.post(reverse("meso:roster_add_self"))

        assert tour.tour_status(coach)["step"] == 1  # profile

    def test_self_designer_advances_on_plan_create(self, client):
        coach = _coach()
        CoachAthlete.add_self(coach)
        tour.set_step(coach.coach_profile, 2)  # designer
        client.force_login(coach)

        client.post(reverse("meso:plan_create", args=[coach.pk]))

        assert tour.tour_status(coach)["step"] == 3  # deliver

    def test_self_deliver_advances_on_plan_deliver(self, client):
        coach = _coach()
        s = _self_plan(coach)  # a deliverable current week
        tour.set_step(coach.coach_profile, 3)  # deliver
        client.force_login(coach)

        client.post(reverse("meso:api_plan_deliver", kwargs={"plan_id": s.plan.pk}))

        assert tour.tour_status(coach)["step"] == 4  # results

    def test_self_results_advances_on_log_session(self, client):
        coach = _coach()
        s = _self_plan(coach, delivered=True)  # a delivered, loggable session
        tour.set_step(coach.coach_profile, 4)  # results
        client.force_login(coach)

        client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
            data=json.dumps({"sets": []}),
            content_type="application/json",
        )

        assert tour.tour_status(coach)["step"] == 5  # groups

    def test_self_results_does_not_advance_on_pending_log(self, client):
        # A pending "save progress" post isn't a completed result — the results
        # step must stay put (Codex #441 P3-5).
        coach = _coach()
        s = _self_plan(coach, delivered=True)
        tour.set_step(coach.coach_profile, 4)  # results
        client.force_login(coach)

        client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
            data=json.dumps({"sets": [], "status": "pending"}),
            content_type="application/json",
        )

        assert tour.tour_status(coach)["step"] == 4  # unchanged

    def test_self_groups_advances_on_group_create(self, client):
        coach = _coach()
        tour.set_step(coach.coach_profile, 5)  # groups
        client.force_login(coach)

        client.post(reverse("meso:group_create"), {"name": "Squad"})

        assert tour.tour_status(coach)["step"] == 6  # agent

    def test_self_agent_advances_on_draft_plan_create(self, client):
        coach = _coach()
        CoachAthlete.add_self(coach)
        tour.set_step(coach.coach_profile, 6)  # agent
        client.force_login(coach)

        client.post(reverse("meso:plan_create", args=[coach.pk]), {"draft": "agent"})

        assert tour.tour_status(coach)["step"] == 7  # finish (terminal)

    def test_self_designer_advances_on_ai_draft_from_designer_step(self, client):
        # The visible "Draft with AI" control sends draft=agent, but a coach
        # parked on the designer step who uses it still creates their self plan —
        # the designer step must advance, not no-op on the agent key (#441 P3-5).
        coach = _coach()
        CoachAthlete.add_self(coach)
        tour.set_step(coach.coach_profile, 2)  # designer
        client.force_login(coach)

        client.post(reverse("meso:plan_create", args=[coach.pk]), {"draft": "agent"})

        assert tour.tour_status(coach)["step"] == 3  # deliver

    def test_self_designer_does_not_advance_creating_another_athletes_plan(
        self, client
    ):
        # A coach who also coaches others, parked on their own designer step,
        # builds a program for a *different* athlete — their self tour must not
        # skip forward (Codex #441 P3-5): their self-link still has no plan.
        coach = _coach()
        CoachAthlete.add_self(coach)
        other = CoachAthleteFactory(coach=coach)  # a non-self athlete
        tour.set_step(coach.coach_profile, 2)  # designer
        client.force_login(coach)

        client.post(reverse("meso:plan_create", args=[other.athlete.pk]))

        assert tour.tour_status(coach)["step"] == 2  # unchanged

    # -- sandbox variant --------------------------------------------------

    def test_sandbox_welcome_advances_on_demo_load_athletes(self, client):
        coach = sandbox.create_sandbox()  # armed at step 0 (welcome)
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "athletes"})

        assert tour.tour_status(coach)["step"] == 1  # profile

    def test_sandbox_designer_advances_on_demo_load_program(self, client):
        coach = sandbox.create_sandbox()
        tour.set_step(CoachProfile.objects.get(user=coach), 2)  # designer
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "program"})

        assert tour.tour_status(coach)["step"] == 3  # deliver

    def test_sandbox_deliver_advances_on_demo_load_delivery(self, client):
        coach = sandbox.create_sandbox()
        tour.set_step(CoachProfile.objects.get(user=coach), 3)  # deliver
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "delivery"})

        assert tour.tour_status(coach)["step"] == 4  # results

    def test_sandbox_results_advances_on_demo_load_log(self, client):
        coach = sandbox.create_sandbox()
        tour.set_step(CoachProfile.objects.get(user=coach), 4)  # results
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "log"})

        assert tour.tour_status(coach)["step"] == 5  # groups

    def test_sandbox_groups_advances_on_demo_load_group(self, client):
        coach = sandbox.create_sandbox()
        tour.set_step(CoachProfile.objects.get(user=coach), 5)  # groups
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "group"})

        assert tour.tour_status(coach)["step"] == 6  # agent

    # -- negative: no skip, no advance off-step or when not touring --------

    def test_action_off_its_step_does_not_advance(self, client):
        # Deliver fired while parked on designer must not skip the coach forward.
        coach = _coach()
        s = _self_plan(coach)
        tour.set_step(coach.coach_profile, 2)  # designer, not deliver
        client.force_login(coach)

        client.post(reverse("meso:api_plan_deliver", kwargs={"plan_id": s.plan.pk}))

        assert tour.tour_status(coach)["step"] == 2  # unchanged

    def test_non_touring_coach_is_left_untouched(self, client):
        # No live tour → the action still performs, but tour_state stays empty.
        coach = _coach()
        s = _self_plan(coach)
        client.force_login(coach)

        client.post(reverse("meso:api_plan_deliver", kwargs={"plan_id": s.plan.pk}))

        assert tour.tour_status(coach) == {}


class TestAdvanceSelfStepGate:
    """The advance gate waits for the step's own self predicate (Codex #441 P3-5).

    ``advance_self_step_if_complete`` only advances once the step's completion
    predicate holds. The deliver/log endpoints are shared with a coach's real
    coaching of other athletes (and the logger can save a ``pending`` draft), so
    a parked-step check alone would let a foreign/incomplete action skip the
    coach's own tour.
    """

    def test_deliver_gate_waits_for_own_delivery(self):
        coach = _coach()
        s = _self_plan(coach)  # a self plan, but its week is NOT yet delivered
        tour.set_step(coach.coach_profile, 3)  # deliver

        # Parked on deliver, but no self delivery exists → refuses to advance.
        assert tour.advance_self_step_if_complete(coach, "deliver") is False
        assert tour.tour_status(coach)["step"] == 3

        s.week.delivered_at = timezone.now()
        s.week.save(update_fields=["delivered_at"])
        assert tour.advance_self_step_if_complete(coach, "deliver") is True
        assert tour.tour_status(coach)["step"] == 4

    def test_results_gate_waits_for_done_own_log(self):
        coach = _coach()
        s = _self_plan(coach, delivered=True)
        tour.set_step(coach.coach_profile, 4)  # results

        # A pending draft doesn't satisfy the predicate → no advance.
        log = SessionLog.objects.create(
            session=s.session, athlete=coach, status=SessionLog.Status.PENDING
        )
        assert tour.advance_self_step_if_complete(coach, "results") is False
        assert tour.tour_status(coach)["step"] == 4

        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])
        assert tour.advance_self_step_if_complete(coach, "results") is True
        assert tour.tour_status(coach)["step"] == 5
