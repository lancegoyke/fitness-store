"""Onboarding UAT round 2: #644 (trial choice) and #642 (invite) through signup.

Both flows cross allauth's signup/login, which can flush the session, so the
#644 choice rides in ``next`` as well as the session; the #642 invite is
remembered in the session only (set by the anonymous claim page of that token,
which is what lets an authenticated GET auto-accept without a crafted link
being able to force an accept).
"""

import re
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "S3cure-pass-phrase-9"


def _hrefs(html):
    return re.findall(r'href="([^"]+)"', html)


def _next_of(href):
    return parse_qs(urlparse(href.replace("&amp;", "&")).query)["next"][0]


def _msgs(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


# ---------------------------------------------------------------------------
# #644 — the trial choice
# ---------------------------------------------------------------------------


class TestTrialChoiceThroughSignup:
    def _cta_hrefs(self, client, plan):
        resp = client.get(reverse("meso:become_coach"), {"plan": plan})
        html = resp.content.decode()
        signup = next(
            h
            for h in _hrefs(html)
            if h.startswith(reverse("account_signup")) and "next=" in h
        )
        login = next(
            h
            for h in _hrefs(html)
            if h.startswith(reverse("account_login")) and "next=" in h
        )
        return signup, login

    def test_cta_links_carry_plan_in_next(self, client):
        signup, login = self._cta_hrefs(client, "trial")
        for href in (signup, login):
            nxt = urlparse(_next_of(href))
            assert nxt.path == reverse("meso:become_coach")
            assert parse_qs(nxt.query)["plan"] == ["trial"]
            assert "plan_sig" in parse_qs(nxt.query)

    def test_cta_without_plan_stays_plain(self, client):
        resp = client.get(reverse("meso:become_coach"))
        signup = next(
            h
            for h in _hrefs(resp.content.decode())
            if h.startswith(reverse("account_signup")) and "next=" in h
        )
        assert _next_of(signup) == reverse("meso:become_coach")

    def test_trial_survives_session_loss_across_signup(self, client):
        signup, _ = self._cta_hrefs(client, "trial")
        nxt = _next_of(signup)
        # A stale/other-account cookie makes allauth's login() flush the
        # session, taking meso_coach_intent with it.
        s = client.session
        s.flush()
        s.save()
        client.cookies.pop("sessionid", None)
        resp = client.post(
            reverse("account_signup"),
            {
                "email": "newcoach@example.com",
                "password1": PASSWORD,
                "name": "New Coach",
                "next": nxt,
            },
        )
        assert resp.status_code == 302
        assert resp.url == nxt
        resp = client.get(resp.url)
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        user = User.objects.get(email="newcoach@example.com")
        assert CoachProfile.objects.filter(user=user).exists()
        sub = CoachSubscription.objects.get(coach=user)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert any("14-day free trial has" in m for m in _msgs(resp))

    def _signed(self, client, plan):
        signup, _ = self._cta_hrefs(client, plan)
        return _next_of(signup)

    def test_free_choice_creates_profile_without_subscription(self, client):
        url = self._signed(client, "free")
        user = UserFactory()
        client.force_login(user)
        resp = client.get(url)
        assert resp.url == reverse("meso:roster")
        assert CoachProfile.objects.filter(user=user).exists()
        assert not CoachSubscription.objects.filter(coach=user).exists()

    def test_get_plan_is_idempotent_for_a_trialed_coach(self, client):
        url = self._signed(client, "trial")
        user = UserFactory()
        client.force_login(user)
        client.get(url)
        sub = CoachSubscription.objects.get(coach=user)
        resp = client.get(url)
        assert resp.url == reverse("meso:roster")  # existing coach: just the roster
        assert CoachSubscription.objects.get(coach=user).pk == sub.pk

    def test_bare_or_forged_plan_does_not_start_anything(self, client):
        url = self._signed(client, "free")
        client = Client()  # no session intent: only the link is in play
        user = UserFactory()
        client.force_login(user)
        for forged in (
            reverse("meso:become_coach") + "?plan=trial",
            url.replace("plan=free", "plan=trial"),
            url + "x",
        ):
            assert client.get(forged).status_code == 200
        assert not CoachProfile.objects.filter(user=user).exists()
        assert not CoachSubscription.objects.filter(coach=user).exists()

    def test_junk_plan_ignored(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.get(reverse("meso:become_coach"), {"plan": "enterprise"})
        assert resp.status_code == 200
        assert not CoachProfile.objects.filter(user=user).exists()

    def test_get_plan_wins_over_session(self, client):
        client.get(reverse("meso:become_coach"), {"plan": "free"})
        url = self._signed(client, "trial")
        client.get(reverse("meso:become_coach"), {"plan": "free"})
        user = UserFactory()
        client.force_login(user)  # force_login keeps the session data
        client.get(url)
        assert CoachSubscription.objects.filter(
            coach=user, status=CoachSubscription.Status.TRIALING
        ).exists()
        assert "meso_coach_intent" not in client.session


# ---------------------------------------------------------------------------
# #642 — the invite
# ---------------------------------------------------------------------------


def _invite(coach=None, **kw):
    coach = coach or UserFactory(name="Sam Rivera")
    kw.setdefault("email", "jordan.ellis@example.com")
    kw.setdefault("label", "Jordan Ellis")
    invite, _ = CoachInvite.open_for(coach=coach, **kw)
    return invite


def _claim(token):
    return reverse("meso:invite_claim", kwargs={"token": token})


class TestClaimPageFlag:
    def test_anonymous_claim_sets_flag(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        assert client.session["meso_claim_token"] == str(invite.token)

    def test_unclaimable_claim_sets_no_flag(self, client):
        invite = _invite()
        invite.revoke()
        client.get(_claim(invite.token))
        assert "meso_claim_token" not in client.session


class TestSignupPageForInvite:
    def test_prefills_and_hides_store_chrome(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        resp = client.get(reverse("account_signup"), {"next": _claim(invite.token)})
        html = resp.content.decode()
        assert resp.status_code == 200
        assert "Join Sam Rivera on Meso" in html
        assert 'value="Jordan Ellis"' in html
        assert 'value="jordan.ellis@example.com"' in html
        assert 'class="nav"' not in html
        assert "newsletter" not in html.lower()

    def test_plain_signup_unchanged(self, client):
        html = client.get(reverse("account_signup")).content.decode()
        assert "Create your account" in html
        assert 'class="nav"' in html
        assert "jordan.ellis" not in html

    @pytest.mark.parametrize("how", ["revoked", "expired", "unknown"])
    def test_stale_flag_is_ordinary_signup(self, client, how):
        invite = _invite()
        client.get(_claim(invite.token))
        if how == "revoked":
            invite.revoke()
        elif how == "expired":
            invite.expires_at = invite.created_at
            invite.save(update_fields=["expires_at"])
        else:
            s = client.session
            s["meso_claim_token"] = "00000000-0000-0000-0000-000000000000"
            s.save()
        html = client.get(reverse("account_signup")).content.decode()
        assert "Create your account" in html
        assert "Join Sam Rivera" not in html
        assert "jordan.ellis@example.com" not in html


class TestInviteThroughSignupAndLogin:
    def test_signup_with_changed_email_auto_accepts(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        resp = client.post(
            reverse("account_signup"),
            {
                "email": "jordan.other@example.com",
                "password1": PASSWORD,
                "name": "Jordan Ellis",
                "next": _claim(invite.token),
            },
        )
        assert resp.url == _claim(invite.token)
        resp = client.get(resp.url)
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")
        athlete = User.objects.get(email="jordan.other@example.com")
        link = CoachAthlete.objects.get(coach=invite.coach, athlete=athlete)
        assert link.is_active
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert "meso_claim_token" not in client.session
        assert any("You're now training with Sam Rivera." in m for m in _msgs(resp))

    def test_login_of_existing_user_auto_accepts(self, client):
        invite = _invite()
        athlete = UserFactory(email="existing@example.com")
        athlete.set_password(PASSWORD)
        athlete.save()
        client.get(_claim(invite.token))
        resp = client.post(
            reverse("account_login"),
            {
                "login": "existing@example.com",
                "password": PASSWORD,
                "next": _claim(invite.token),
            },
        )
        assert resp.url == _claim(invite.token)
        resp = client.get(resp.url)
        assert resp.url == reverse("meso:athlete_home")
        assert CoachAthlete.objects.get(coach=invite.coach, athlete=athlete).is_active

    def test_authenticated_get_without_flag_does_not_accept(self, client):
        invite = _invite()
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.get(_claim(invite.token))
        assert resp.status_code == 200
        assert b"Accept invite" in resp.content
        assert b"Decline" in resp.content
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()

    def test_flag_for_other_token_does_not_accept(self, client):
        a = _invite()
        b = _invite(email="b@example.com", label="B")
        client.get(_claim(a.token))
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.get(_claim(b.token))
        assert resp.status_code == 200
        assert b"Accept invite" in resp.content
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()

    def test_coach_with_flag_is_not_self_accepted(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        client.force_login(invite.coach)
        resp = client.get(_claim(invite.token))
        assert resp.status_code == 200
        assert not CoachAthlete.objects.filter(athlete=invite.coach).exists()

    def test_over_limit_coach_parks_waiting_neutrally(self, client):
        coach = UserFactory(name="Sam Rivera")
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT):
            CoachAthleteFactory(coach=coach)
        invite = _invite(coach=coach)
        client.get(_claim(invite.token))
        athlete = UserFactory()
        client.force_login(athlete)
        resp = client.get(_claim(invite.token))
        assert resp.url == reverse("meso:athlete_home")
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.is_waiting
        msgs = " ".join(_msgs(resp)).lower()
        assert "connected to sam rivera" in msgs
        assert "upgrade" not in msgs

    def test_decline_clears_flag(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        client.force_login(UserFactory())
        client.post(_claim(invite.token), {"action": "decline"})
        assert "meso_claim_token" not in client.session
