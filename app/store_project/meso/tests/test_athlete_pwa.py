"""Athlete slice Phase 4b — the installable PWA shell (decision S7).

The athlete surface becomes installable and offline-tolerant: a web-app
manifest, a service worker served at ``/meso/`` scope, and an offline fallback
page. These tests pin the *server-side contract* of the PWA — the pieces a
browser needs to install the app and open it offline:

- the manifest is valid JSON with the install fields (name, ``standalone``,
  ``start_url`` = the athlete home, ``/meso/`` scope, an icon);
- the service worker is served from ``/meso/sw.js`` (so its default scope is
  ``/meso/``) with a JavaScript content type and the ``Service-Worker-Allowed``
  header, and precaches the shell + offline page;
- the offline page renders without auth (the SW caches it on install, so it must
  not redirect to login);
- the athlete templates link the manifest and register the worker, while the
  coach roster does **not** (the PWA is the athlete surface, mirroring Phase 1's
  athlete-only nav blocks);
- replaying the same log POST is idempotent — the guarantee that makes the
  client's offline queue safe to flush on reconnect.

The service worker's offline *queue* and the install UX are browser-side and
verified manually; what is pinned here is everything Django serves.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(*, athlete=None, coach=None):
    """A delivered plan → week → session → prescription for one athlete."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso, index=2, is_current=True, delivered_at=timezone.now()
    )
    session = SessionFactory(week=week, day_number=1, name="Lower", bias="Quad")
    presc = ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return athlete, coach, session, presc


MANIFEST = reverse("meso:manifest")
SW = reverse("meso:service_worker")
OFFLINE = reverse("meso:offline")
HOME = reverse("meso:athlete_home")
ROSTER = reverse("meso:roster")


class TestManifest:
    def test_served_as_manifest_json(self, client):
        resp = client.get(MANIFEST)
        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("application/manifest+json")

    def test_install_fields(self, client):
        data = json.loads(client.get(MANIFEST).content)
        assert data["name"]
        assert data["short_name"]
        assert data["display"] == "standalone"
        # Launch straight into the athlete's training surface, scoped to /meso/.
        assert data["start_url"] == HOME
        assert data["scope"] == "/meso/"
        assert data["icons"], "manifest must declare at least one icon"
        assert data["theme_color"]
        assert data["background_color"]

    def test_no_login_required(self, client):
        # The manifest is fetched by the browser before any session — never gated.
        assert client.get(MANIFEST).status_code == 200


class TestServiceWorker:
    def test_served_as_javascript(self, client):
        resp = client.get(SW)
        assert resp.status_code == 200
        assert "javascript" in resp["Content-Type"]

    def test_scope_header_allows_meso(self, client):
        # Served at /meso/sw.js → default scope /meso/; the header makes it explicit.
        resp = client.get(SW)
        assert resp["Service-Worker-Allowed"] == "/meso/"

    def test_precaches_shell_and_offline(self, client):
        body = client.get(SW).content.decode()
        # The worker must know the offline fallback and the athlete home to cache.
        assert OFFLINE in body
        assert HOME in body

    def test_no_login_required(self, client):
        assert client.get(SW).status_code == 200

    def test_runtime_cache_is_gated_to_static_assets(self, client):
        # Dynamic GETs (e.g. /meso/api/.../status/ agent polling) must pass
        # through, never be served stale from Cache Storage — the runtime-cache
        # branch is gated on the static-asset prefix.
        from django.conf import settings

        body = client.get(SW).content.decode()
        assert settings.STATIC_URL in body
        assert "startsWith(STATIC_PREFIX)" in body

    def test_navigation_cache_is_gated_to_athlete_pages(self, client):
        # The athlete-only PWA: coach navigations (/meso/roster/, designer) pass
        # through, only /meso/me/ pages + the offline page are cached/served.
        body = client.get(SW).content.decode()
        assert "startsWith(HOME_URL)" in body


class TestOfflinePage:
    def test_renders_without_auth(self, client):
        # The SW caches this on install, so it must render for an anonymous fetch
        # rather than 302 to login (a cached login redirect would be useless).
        resp = client.get(OFFLINE)
        assert resp.status_code == 200
        assert b"offline" in resp.content.lower()


class TestTemplateWiring:
    def test_home_links_manifest_and_registers_sw(self, client):
        athlete, *_ = seed()
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert MANIFEST in body
        assert 'rel="manifest"' in body
        # The page registers the worker.
        assert SW in body
        assert "serviceWorker" in body

    def test_session_links_manifest(self, client):
        athlete, _c, session, _p = seed()
        client.force_login(athlete)
        url = reverse("meso:athlete_session", kwargs={"pk": session.pk})
        body = client.get(url).content.decode()
        assert MANIFEST in body
        assert SW in body

    def test_coach_roster_is_not_a_pwa(self, client):
        # The PWA is the athlete surface; a coach screen stays plain (no manifest,
        # no worker), mirroring Phase 1's athlete-only nav blocks.
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        client.force_login(coach)
        body = client.get(ROSTER).content.decode()
        assert 'rel="manifest"' not in body
        assert SW not in body


class TestOfflineReplayIsIdempotent:
    """Replaying a queued log POST must not duplicate or corrupt the log.

    The client's offline queue stashes failed saves and flushes them on
    reconnect; if the same save was also retried online, the endpoint sees the
    payload twice. Re-posting must converge to one log with the replayed sets.
    """

    def test_double_post_yields_one_log(self, client):
        athlete, _c, session, presc = seed()
        client.force_login(athlete)
        url = reverse("meso:athlete_log_session", kwargs={"pk": session.pk})
        payload = {
            "status": "done",
            "date": "2026-06-20",
            "sets": [
                {
                    "prescription": presc.pk,
                    "set_number": 1,
                    "reps": "6",
                    "load": "72",
                    "rpe": "8",
                },
                {
                    "prescription": presc.pk,
                    "set_number": 2,
                    "reps": "6",
                    "load": "72",
                    "rpe": "8",
                },
            ],
        }
        first = client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )
        assert first.status_code == 200
        replay = client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )
        assert replay.status_code == 200

        logs = SessionLog.objects.filter(session=session, athlete=athlete)
        assert logs.count() == 1
        log = logs.get()
        assert log.status == SessionLog.Status.DONE
        assert log.date.isoformat() == "2026-06-20"
        assert log.sets.count() == 2
