"""Guided demo onboarding tour — Phase 2 (issue #430).

Phase 1 (``test_demo_segments.py``) split ``load_demo`` into idempotent
per-feature segment loaders. This phase drives them from an in-app guided
tour and flips ``create_sandbox`` to an **empty start** (the tour populates
the workspace step by step instead). Covers:

- ``tour.STEPS`` data sanity (every ``url_name`` reverses, every named
  segment exists in ``meso_demo.SEGMENTS``);
- the ``tour.py`` helpers directly (``tour_status``/``is_active``/
  ``start_tour``/``set_step``/``dismiss``/``complete``/``build_config``);
- ``create_sandbox``'s empty-start flip: a fresh sandbox has no demo data and
  an active tour parked at step 0;
- the roster's tour mount + embedded config JSON — present for an active
  sandbox coach, absent for a real coach, absent once dismissed/completed,
  and the static "Get started" card is suppressed while touring;
- ``meso:tour_state`` (advance/back/goto clamp + persist, dismiss/complete
  persist, restart resets, anonymous -> login, AJAX vs. plain-POST shape);
- ``meso:tour_skip`` (the O6 "skip · load everything" shortcut): loads the
  full aggregate demo and marks the tour complete.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso import tour
from store_project.meso.models import CoachProfile
from store_project.users.factories import UserFactory

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

    def test_every_named_segment_exists_in_demo_segments(self):
        for step in tour.STEPS:
            if step["segment"] is not None:
                assert step["segment"] in demo.SEGMENTS

    def test_eight_steps(self):
        assert len(tour.STEPS) == 8

    def test_step_keys_are_unique(self):
        keys = [step["key"] for step in tour.STEPS]
        assert len(keys) == len(set(keys))


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
    def test_none_without_a_coach_profile(self):
        user = UserFactory()
        assert tour.build_config(user) is None

    def test_carries_all_steps_with_resolved_urls(self):
        coach = _coach()
        config = tour.build_config(coach)
        assert len(config["steps"]) == len(tour.STEPS)
        for step, spec in zip(config["steps"], tour.STEPS):
            assert step["key"] == spec["key"]
            assert step["url"] == reverse(spec["url_name"])

    def test_loaded_flags_start_false_and_flip_with_the_segment(self):
        coach = _coach()
        config = tour.build_config(coach)
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["loaded"] is False

        demo.load_athletes(coach)

        config = tour.build_config(coach)
        welcome = next(s for s in config["steps"] if s["key"] == "welcome")
        assert welcome["loaded"] is True

    def test_steps_with_no_segment_have_no_loaded_flag(self):
        coach = _coach()
        config = tour.build_config(coach)
        profile_step = next(s for s in config["steps"] if s["key"] == "profile")
        assert profile_step["loaded"] is None

    def test_endpoints_and_current_progress(self):
        coach = _coach()
        tour.set_step(coach.coach_profile, 2)

        config = tour.build_config(coach)

        assert config["step"] == 2
        assert config["status"] == "active"
        assert config["state_url"] == reverse("meso:tour_state")
        assert config["skip_url"] == reverse("meso:tour_skip")
        assert config["demo_load_url"] == reverse("meso:demo_load")
        assert config["signup_url"] == reverse("meso:sandbox_signup")


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
        coach = _coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()

        assert 'id="meso-tour"' not in body
        assert 'id="meso-tour-config"' not in body

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
