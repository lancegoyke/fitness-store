import datetime
import ipaddress
import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from store_project.notifications.emails import send_week_delivered_email

from . import presenters
from . import push as meso_push
from .agent import apply as agent_apply
from .agent import client as agent_client
from .agent import jobs as agent_jobs
from .agent import service as agent_service
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachProfile
from .models import ExercisePrescription
from .models import LoggedSet
from .models import MesoGroup
from .models import Plan
from .models import ProposedChange
from .models import PushSubscription
from .models import Session
from .models import SessionLog
from .models import WeekDelivery
from .serializers import current_week
from .serializers import serialize_chat_thread
from .serializers import serialize_plan
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change
from .serializers import serialize_session_log
from .serializers import serialize_week_snapshot

logger = logging.getLogger(__name__)


def _coach_working_plan(user, *, plans=None):
    """The coach's most-recently-touched, non-archived plan, or None.

    The target a bare ``/meso/designer/`` or ``/meso/deliver/`` URL resolves to:
    the plan the coach last worked, or back on the roster if they have none.
    ``plans`` scopes the candidate set — the *designer* passes
    ``Plan.objects.editable_by(user)`` so a group shared program can be the
    working plan too (the designer handles both kinds), while deliver keeps the
    individual-only default (``for_coach``) since deliver-to-all is Phase 4.
    """
    qs = plans if plans is not None else Plan.objects.for_coach(user)
    return qs.exclude(status=Plan.Status.ARCHIVED).order_by("-modified").first()


def _coach_session_or_404(user, pk):
    """A session on a plan the coach owns (active relationship), or ``Http404``.

    The coach-side analogue of ``_athlete_session_or_404``: a foreign athlete's
    session or an unknown id are an indistinguishable flat 404 (no leak). Used by
    the results screen; delivery isn't required — a logged session is logged.
    """
    session = (
        Session.objects.filter(
            pk=pk, week__mesocycle__plan__in=Plan.objects.for_coach(user)
        )
        .select_related("week__mesocycle__plan__relationship")
        .prefetch_related("prescriptions")
        .first()
    )
    if session is None:
        raise Http404("Unknown session")
    return session


def _coach_latest_logged_session(user):
    """The coach's most-recently *completed* session across their athletes, or None.

    The target the bare ``/meso/results/`` resolves to. Only *done* logs count —
    a pending draft isn't a result yet (the results screen would render it as an
    awaiting session anyway). Ordered by the workout date (then created) so the
    coach lands on the session most recently trained.
    """
    log = (
        SessionLog.objects.filter(
            session__week__mesocycle__plan__in=Plan.objects.for_coach(user),
            status=SessionLog.Status.DONE,
        )
        .select_related("session")
        .order_by("-date", "-created_at")
        .first()
    )
    return log.session if log else None


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso strength-training program designer.

    A self-contained, full-screen coach tool. The view serializes a real, owned
    plan into the page and the Alpine front-end hydrates from it (then autosaves
    edits to the API endpoints below). The bare URL has no fixtures anymore — it
    redirects to the coach's working plan (or the roster). The agent column is
    live (agent slice) and its conversation is persisted: ``chat_thread``
    rebuilds the thread from the plan's proposal batches so it survives a reload.
    """

    template_name = "meso/designer.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("plan_id") is None:
            # The designer opens individual *or* group plans, so its bare-URL
            # target spans both (``editable_by``) — a coach who just edited a
            # group's shared program lands back on it, not an older individual one.
            plan = _coach_working_plan(
                request.user, plans=Plan.objects.editable_by(request.user)
            )
            if plan is None:
                messages.info(request, "Pick an athlete to start a program.")
                return redirect("meso:roster")
            return redirect("meso:designer_plan", plan_id=plan.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # ``editable_by`` (not ``for_coach``) so a *group* shared program — rooted
        # at a group the coach owns — opens here too, not just individual plans.
        plan = (
            Plan.objects.editable_by(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        ctx["plan_data"] = serialize_plan(plan)
        # The persisted agent conversation, rebuilt from this plan's proposal
        # batches so the chat survives a reload (the JS hydrates ``messages``
        # from it, falling back to the greeting when empty).
        ctx["chat_thread"] = serialize_chat_thread(plan)
        return ctx


class RosterView(LoginRequiredMixin, TemplateView):
    """Front door: the coach's athletes (scoped to active relationships).

    A *pure* athlete — someone with an active coach link but no ``CoachProfile``
    — has no roster of their own, so they're sent to their training surface
    instead. A coach (or a coach who also trains) keeps the roster.
    """

    template_name = "meso/roster.html"

    def get(self, request, *args, **kwargs):
        is_coach = CoachProfile.objects.filter(user=request.user).exists()
        is_athlete = CoachAthlete.objects.for_athlete(request.user).active().exists()
        if not is_coach and is_athlete:
            return redirect("meso:athlete_home")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        links = (
            CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .select_related("athlete", "athlete__athlete_profile")
            .prefetch_related("athlete__contraindications")
            .order_by("athlete__name", "athlete__email")
        )
        athletes = [presenters.roster_athlete(link.athlete) for link in links]
        groups = (
            MesoGroup.objects.for_coach(self.request.user)
            .active()
            .prefetch_related("memberships__relationship__athlete")
        )
        ctx["active"] = "roster"
        ctx["athletes"] = athletes
        # Groups (S1 Phase 1) read real rows; the shared program + per-athlete
        # auto-adjusts land in groups Phase 2/3. Activity (Phase 3) needs logged
        # sessions; needs-review (Phase 2) needs agent state. Empty until then.
        ctx["groups"] = [presenters.roster_group(g) for g in groups]
        ctx["activity"] = []
        ctx["needs_review"] = 0
        return ctx


class AthleteProfileView(LoginRequiredMixin, TemplateView):
    """Full athlete record — only viewable by a coach with an active link."""

    template_name = "meso/athlete_profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        link = (
            CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .select_related("athlete", "athlete__athlete_profile")
            .prefetch_related("athlete__contraindications")
            .filter(athlete_id=kwargs["pk"])
            .first()
        )
        if link is None:
            raise Http404("Unknown athlete")
        ctx["active"] = "roster"
        ctx["athlete"] = presenters.profile_athlete(link.athlete)
        ctx["coach_style"] = presenters.coach_style(self.request.user)
        # Current block / macrocycle / latest results arrive with the program
        # schema (Phase 2) and logging (Phase 3).
        ctx["macrocycle"] = []
        ctx["results_summary"] = None
        return ctx


class GroupDetailView(LoginRequiredMixin, TemplateView):
    """A coach's training group — members + their cross-group flags (S1 Phase 1).

    Coach-scoped: a foreign or unknown group is a flat 404. The shared program +
    per-athlete auto-adjusts land in groups Phase 2/3; this is the read surface.
    """

    template_name = "meso/group_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        group = (
            MesoGroup.objects.for_coach(self.request.user)
            .filter(pk=kwargs["pk"])
            .first()
        )
        if group is None:
            raise Http404("Unknown group")
        ctx["active"] = "roster"
        ctx["group"] = presenters.group_detail(group)
        return ctx


@login_required
@require_POST
def group_design(request, pk):
    """Open (or create) a group's shared program and land in the designer (Phase 2).

    Coach-scoped: a foreign or unknown group is a flat 404. Idempotent — reuses
    the group's existing non-archived shared plan, only creating (with a starter
    scaffold) when there is none. The lookup + create run under a row lock on the
    group so two concurrent submits can't both see "no plan" and each create one
    (the second waits, then reuses the first's plan).
    """
    with transaction.atomic():
        group = (
            MesoGroup.objects.select_for_update()
            .for_coach(request.user)
            .filter(pk=pk)
            .first()
        )
        if group is None:
            raise Http404("Unknown group")
        plan = group.shared_plan() or group.create_shared_plan()
    return redirect("meso:designer_plan", plan_id=plan.pk)


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's own logged-in surface, distinct from the coach's view of an
# athlete (``/meso/athlete/<uuid>/``). Read-only here; logging lands in Phase 2.
# Everything is scoped to the athlete's *active* coaches (``for_athlete``), to
# **delivered** weeks (delivery gates *visibility* — an undelivered week is
# hidden; a delivered week's *current* contents are shown, see
# ``latest_delivered_week``), and to non-archived plans. An out-of-scope session
# is a flat 404 — never a silent empty render.


def _pwa_context():
    """Push install config for the athlete templates (Phase 4b — S7).

    ``push_enabled`` gates the subscribe affordance + VAPID key in the template;
    with no keys configured the PWA still installs and logs offline, it just
    won't offer push.
    """
    return {
        "push_enabled": meso_push.push_enabled(),
        "vapid_public_key": meso_push.vapid_public_key(),
    }


def _athlete_plans(user):
    """Plans the athlete may see: active-coach, non-archived (D-a)."""
    return Plan.objects.for_athlete(user).exclude(status=Plan.Status.ARCHIVED)


def _athlete_session_or_404(user, pk):
    """A delivered session the athlete owns, or ``Http404``.

    404 unless the session's week is delivered *and* its plan is one the athlete
    reaches through an active coach link — a foreign athlete, an undelivered
    week, an archived plan, or an unknown id are indistinguishable (no leak).
    """
    session = (
        Session.objects.filter(
            pk=pk,
            week__delivered_at__isnull=False,
            week__mesocycle__plan__in=_athlete_plans(user),
        )
        .select_related("week__mesocycle__plan__relationship")
        .prefetch_related("prescriptions")
        .first()
    )
    if session is None:
        raise Http404("Unknown session")
    return session


class AthleteHomeView(LoginRequiredMixin, TemplateView):
    """The athlete's training home: their delivered programs, this week."""

    template_name = "meso/athlete_home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "training"
        ctx["plans"] = presenters.athlete_home(self.request.user)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        ctx.update(_pwa_context())
        return ctx


class AthleteSessionView(LoginRequiredMixin, TemplateView):
    """One delivered session — the athlete's interactive logger (Phase 2).

    Renders the prescribed grid as set-input rows pre-filled from the athlete's
    own existing log, and injects ``log_data`` for the Alpine logger to hydrate
    from and POST back to ``athlete_log_session``.
    """

    template_name = "meso/athlete_session.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = _athlete_session_or_404(self.request.user, kwargs["pk"])
        sess = presenters.athlete_session(session, self.request.user)
        ctx["active"] = "training"
        ctx["session"] = sess
        ctx["log_data"] = presenters.athlete_log_payload(sess)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        ctx.update(_pwa_context())
        return ctx


# Free-form text cells per logged set, mapped to their model ``max_length``.
LOG_SET_FIELDS = {"reps": 32, "load": 32, "rpe": 32}
# A generous ceiling on a set's number — no real session has this many sets, and
# bounding it here stops a malformed client from storing an enormous ``set_number``
# that would later balloon the session page's set-row render (presenters._set_rows).
MAX_LOGGED_SET_NUMBER = 50


@login_required
@require_POST
def athlete_log_session(request, pk):
    """Upsert the athlete's log for a delivered session they own (Phase 2).

    Replaces the athlete's own ``SessionLog`` + ``LoggedSet`` rows for this
    session with the posted state, flips the session done (unless an explicit
    ``status`` says otherwise), and stamps the date (today when none is given).
    Scoped by ``_athlete_session_or_404`` — a foreign, undelivered, archived, or
    unknown session is a flat 404, never a silent write. The body is fully
    validated *before* any write, so a bad request is a 400 that persists
    nothing; the write itself is idempotent (re-logging updates the one log,
    replacing its set rows rather than appending). These are the first real rows
    ``serialize_recent_logs`` grounds the agent on.
    """
    session = _athlete_session_or_404(request.user, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    status = payload.get("status", SessionLog.Status.DONE)
    if status not in (SessionLog.Status.PENDING, SessionLog.Status.DONE):
        return HttpResponseBadRequest("status must be 'pending' or 'done'.")

    # An explicit date is honored; a missing one defaults to today only when
    # *creating* the log — re-saving an existing log without a date keeps its
    # original date so editing a set days later doesn't move the workout (which
    # would reorder recent-log grounding). ``explicit_date`` is None when none
    # was sent.
    raw_date = payload.get("date")
    explicit_date = None
    if raw_date not in (None, ""):
        if not isinstance(raw_date, str):
            return HttpResponseBadRequest("date must be an ISO date string.")
        try:
            explicit_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return HttpResponseBadRequest("date must be an ISO date (YYYY-MM-DD).")

    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        return HttpResponseBadRequest("notes must be a string.")

    cleaned_sets, error = _clean_logged_sets(payload.get("sets", []), session)
    if error is not None:
        return error

    with transaction.atomic():
        log = (
            SessionLog.objects.filter(session=session, athlete=request.user)
            .order_by("-created_at")
            .first()
        )
        if log is None:
            log = SessionLog(session=session, athlete=request.user)
        log.status = status
        if explicit_date is not None:
            log.date = explicit_date
        elif log.date is None:  # first save (or a log never dated) → stamp today
            log.date = timezone.localdate()
        # else: a re-save with no date keeps the existing workout date.
        log.notes = notes
        log.save()
        log.sets.all().delete()
        LoggedSet.objects.bulk_create(
            [
                LoggedSet(
                    session_log=log,
                    prescription_id=cs["prescription_id"],
                    set_number=cs["set_number"],
                    reps=cs["reps"],
                    load=cs["load"],
                    rpe=cs["rpe"],
                )
                for cs in cleaned_sets
            ]
        )
    return JsonResponse({"ok": True, "log": serialize_session_log(log)})


def _clean_logged_sets(raw_sets, session):
    """Validate the posted ``sets`` against this session, or return a 400.

    Returns ``(cleaned, None)`` on success or ``(None, HttpResponseBadRequest)``.
    Every set must reference a prescription **in this session** (no foreign rows),
    carry a positive integer ``set_number`` (defaulting to its position), and have
    string reps/load/rpe within the model's ``max_length``.
    """
    if not isinstance(raw_sets, list):
        return None, HttpResponseBadRequest("sets must be a list.")
    allowed_ids = {p.pk for p in session.prescriptions.all()}
    cleaned = []
    seen = set()
    for position, raw in enumerate(raw_sets, start=1):
        if not isinstance(raw, dict):
            return None, HttpResponseBadRequest("Each set must be an object.")
        presc_id = raw.get("prescription")
        # ``bool`` is an ``int`` subclass — reject it explicitly so ``true`` isn't an id.
        if (
            not isinstance(presc_id, int)
            or isinstance(presc_id, bool)
            or presc_id not in allowed_ids
        ):
            return None, HttpResponseBadRequest(
                "Each set must reference a prescription in this session."
            )
        set_number = raw.get("set_number", position)
        if (
            not isinstance(set_number, int)
            or isinstance(set_number, bool)
            or not 1 <= set_number <= MAX_LOGGED_SET_NUMBER
        ):
            return None, HttpResponseBadRequest(
                f"set_number must be between 1 and {MAX_LOGGED_SET_NUMBER}."
            )
        # Each (prescription, set_number) is logged at most once — duplicates
        # would persist as two rows that the presenter collapses on reload but
        # the agent's grounding still double-counts, breaking idempotency.
        key = (presc_id, set_number)
        if key in seen:
            return None, HttpResponseBadRequest(
                "Duplicate set for the same prescription and set number."
            )
        seen.add(key)
        fields = {}
        for field, max_length in LOG_SET_FIELDS.items():
            value = raw.get(field, "")
            if not isinstance(value, str):
                return None, HttpResponseBadRequest(f"{field} must be a string.")
            if len(value) > max_length:
                return None, HttpResponseBadRequest(f"{field} is too long.")
            fields[field] = value
        cleaned.append(
            {"prescription_id": presc_id, "set_number": set_number, **fields}
        )
    return cleaned, None


# -- Athlete PWA: manifest, service worker, offline shell (Phase 4b — S7) --
#
# The athlete surface is an installable, offline-tolerant PWA. The manifest and
# service worker are served as *views* (not static files) for two reasons:
#   1. the static pipeline (``CompressedManifestStaticFilesStorage``) hashes
#      filenames, which would give the worker an unstable URL across deploys; and
#   2. a service worker only controls pages at or below its own path, so it must
#      be served from ``/meso/sw.js`` to control ``/meso/me/``.
# The worker is rendered from a template that resolves the *hashed* asset URLs via
# ``{% static %}`` at render time, so its precache list stays valid every deploy.

PWA_THEME_COLOR = "#3c73c5"  # meso accent (oklch(0.56 0.14 258))
PWA_BACKGROUND_COLOR = "#f5f6f7"  # meso app background (--bg)


@require_GET
def manifest_webmanifest(request):
    """The web-app manifest — the browser's install descriptor (S7).

    Public (the browser fetches it before any session). Launches into the
    athlete home, scoped to ``/meso/`` so only the athlete surface is the app.
    """
    data = {
        "name": "Meso — Training",
        "short_name": "Meso",
        "description": (
            "Your coach's training plan — log every session, even offline."
        ),
        "start_url": reverse("meso:athlete_home"),
        "scope": "/meso/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": PWA_THEME_COLOR,
        "background_color": PWA_BACKGROUND_COLOR,
        "icons": [
            {
                "src": static("png/meso-icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("png/meso-icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("png/meso-icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


# Bumped when the cached shell changes so the worker drops stale caches on
# activate. Keep in sync with the cache name baked into the worker template.
PWA_CACHE_VERSION = "meso-pwa-v1"


@require_GET
def service_worker(request):
    """Serve the athlete service worker from ``/meso/sw.js`` (S7).

    Rendered from a template so its precache list can reference the hashed
    static URLs (``{% static %}``). ``Service-Worker-Allowed`` is set explicitly
    even though the served path already scopes it to ``/meso/``.
    """
    body = render_to_string(
        "meso/sw.js",
        {
            "cache_version": PWA_CACHE_VERSION,
            "offline_url": reverse("meso:offline"),
            "home_url": reverse("meso:athlete_home"),
            "static_url": settings.STATIC_URL,
        },
        request=request,
    )
    resp = HttpResponse(body, content_type="text/javascript")
    resp["Service-Worker-Allowed"] = "/meso/"
    # The worker itself must never be served stale, or a new shell can't ship.
    resp["Cache-Control"] = "no-cache"
    return resp


class OfflineView(TemplateView):
    """The offline fallback the worker caches on install (S7).

    Deliberately login-free: the worker pre-caches it on a cold load, so it must
    render for an anonymous fetch rather than redirect to login (a cached login
    redirect would be a useless fallback).
    """

    template_name = "meso/offline.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "training"
        return ctx


# -- Athlete web push: subscribe / unsubscribe (Phase 4b — S3/S7) ----------


def _is_safe_push_endpoint(endpoint):
    """A plausible browser push endpoint: HTTPS to a public host.

    Hardens the SSRF surface — the stored endpoint is later fetched server-side
    by ``pywebpush`` during delivery, so reject the obvious internal targets
    (non-HTTPS, ``localhost``, private/loopback/link-local/reserved IP literals)
    before persisting. A DNS name is accepted (real push services are named
    hosts); name→private-IP rebinding is out of scope for this gate.
    """
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname
    if host.lower() == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname, not an IP literal — accept
    # One property covers every non-public range (private, loopback, link-local,
    # reserved, CGNAT 100.64/10, documentation, …) — stricter than enumerating.
    return ip.is_global


@login_required
@require_POST
def push_subscribe(request):
    """Store the logged-in athlete's push subscription (upsert by endpoint).

    Body is the browser ``PushSubscription`` JSON (``endpoint`` + ``keys.p256dh``
    / ``keys.auth``). The endpoint is unique: re-subscribing (or a different user
    on the same device) reassigns the row to the current athlete. Validated
    before any write — a malformed body, or an endpoint that isn't HTTPS to a
    public host (SSRF guard), is a 400.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    endpoint = payload.get("endpoint")
    keys = payload.get("keys")
    if not isinstance(endpoint, str) or not endpoint:
        return HttpResponseBadRequest("endpoint is required.")
    if not isinstance(keys, dict):
        return HttpResponseBadRequest("keys is required.")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not p256dh:
        return HttpResponseBadRequest("keys.p256dh is required.")
    if not isinstance(auth, str) or not auth:
        return HttpResponseBadRequest("keys.auth is required.")
    if len(endpoint) > 512 or len(p256dh) > 255 or len(auth) > 255:
        return HttpResponseBadRequest("Subscription fields are too long.")
    if not _is_safe_push_endpoint(endpoint):
        return HttpResponseBadRequest("endpoint must be an https URL to a public host.")

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"athlete": request.user, "p256dh": p256dh, "auth": auth},
    )
    return JsonResponse({"ok": True}, status=201)


@login_required
@require_POST
def push_unsubscribe(request):
    """Drop the athlete's own subscription by endpoint (best-effort).

    Scoped to the caller's rows: an athlete can only remove their own
    subscriptions. An unknown endpoint is a quiet success (idempotent).
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return HttpResponseBadRequest("endpoint is required.")
    PushSubscription.objects.filter(athlete=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


# -- invite / relationship actions ----------------------------------------
#
# Tokened POST endpoints. Email delivery of these links is a follow-up; the
# state machine and authorization live here now. Each action is restricted to
# the party entitled to take it (recipient for accept/decline, either party for
# end), independent of who holds the token URL.


@login_required
@require_POST
def invite_accept(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_pending or request.user != link.recipient():
        return HttpResponseForbidden("You cannot respond to this invite.")
    link.accept()
    messages.success(request, "Relationship accepted.")
    return redirect("meso:roster")


@login_required
@require_POST
def invite_decline(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_pending or request.user != link.recipient():
        return HttpResponseForbidden("You cannot respond to this invite.")
    link.decline()
    messages.success(request, "Invite declined.")
    return redirect("meso:roster")


@login_required
@require_POST
def relationship_end(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_active or request.user not in (link.coach, link.athlete):
        return HttpResponseForbidden("You cannot end this relationship.")
    link.end()
    messages.success(request, "Relationship ended.")
    return redirect("meso:roster")


# -- designer autosave API (Phase 3) --------------------------------------
#
# Plain JSON endpoints (no DRF) the designer grid POSTs edits to. Every call is
# scoped to a plan the requester coaches over an *active* relationship — an
# existing-but-unowned plan is a 403, never a silent no-op (N2 / the plan's
# "non-owner POST → 403"). Children (prescription, session) must belong to that
# plan, or it's a 404.

# Free-form text cells the grid edits, mapped to their model ``max_length``.
PATCHABLE_FIELDS = {
    "name": 255,
    "sets": 32,
    "reps": 32,
    "load": 32,
    "rpe": 32,
    "note": 255,
}


def _coach_plan_or_forbidden(request, plan_id):
    """The plan the requester coaches, or an ``HttpResponseForbidden``.

    404 when the plan does not exist; 403 when it exists but the requester may
    not edit it — for an individual plan, its coach over an active relationship;
    for a group plan, the coach who owns the group (``Plan.is_editable_by``).
    """
    plan = get_object_or_404(Plan, pk=plan_id)
    if not plan.is_editable_by(request.user):
        return None, HttpResponseForbidden("You do not own this plan.")
    return plan, None


def _touch_plan(plan):
    """Bump the plan's ``modified`` so it reads as the coach's working plan.

    The autosave/deliver endpoints write *child* rows (prescriptions, weeks),
    which would otherwise leave ``Plan.modified`` stale — and ``_coach_working_plan``
    orders the bare designer/deliver redirect target by it. ``modified`` is
    ``auto_now``, so saving the field stamps it now.
    """
    plan.save(update_fields=["modified"])


@login_required
@require_POST
def prescription_patch(request, plan_id, pk):
    """Patch one prescription cell (or a small batch of cells)."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    prescription = get_object_or_404(
        ExercisePrescription, pk=pk, session__week__mesocycle__plan=plan
    )
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    updates = {}
    for field, max_length in PATCHABLE_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            return HttpResponseBadRequest(f"{field} must be a string.")
        if len(value) > max_length:
            return HttpResponseBadRequest(f"{field} is too long.")
        updates[field] = value

    if updates:
        for field, value in updates.items():
            setattr(prescription, field, value)
        prescription.save(update_fields=list(updates))
        _touch_plan(plan)
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}
    )


@login_required
@require_POST
def session_add_exercise(request, plan_id, pk):
    """Append a blank prescription row to a session."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    session = get_object_or_404(Session, pk=pk, week__mesocycle__plan=plan)
    next_order = (session.prescriptions.aggregate(m=Max("order"))["m"] or 0) + 1
    prescription = ExercisePrescription.objects.create(
        session=session,
        name="New exercise",
        order=next_order,
        sets="3",
        reps="10",
        load="",
        rpe="7",
        note="",
    )
    _touch_plan(plan)
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}, status=201
    )


@login_required
@require_POST
def plan_deliver(request, plan_id):
    """Deliver the plan's current week: stamp ``delivered_at`` + snapshot it (Phase 4)."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    if plan.is_group:
        # Deliver-to-all fans a per-athlete snapshot out to each member; that is
        # groups Phase 4. Until then a group plan has no single athlete to deliver
        # to, so reject it here rather than crash on ``plan.athlete``.
        return HttpResponseBadRequest("Group delivery isn't available yet.")
    week = current_week(plan)
    if week is None:
        return HttpResponseBadRequest("This plan has no week to deliver.")
    now = timezone.now()
    week.delivered_at = now
    week.save(update_fields=["delivered_at"])
    WeekDelivery.objects.create(
        week=week, delivered_at=now, payload=serialize_week_snapshot(week)
    )
    _touch_plan(plan)
    _notify_athlete_delivered(request, plan, week)
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "week": {"id": week.pk, "label": f"Wk {week.index}"},
        },
        status=201,
    )


def _notify_athlete_delivered(request, plan, week):
    """Best-effort: email **and** push the athlete that ``week`` was delivered.

    S3 (email, Phase 4a) + S7 (web push, Phase 4b). Deferred to
    ``transaction.on_commit`` so it fires only after the delivery actually
    commits — under ``ATOMIC_REQUESTS`` the view runs in a transaction, and a
    rolled-back deliver must not notify a false "your week is ready". Each
    channel is independently best-effort: a failure in one is swallowed and
    logged, never a 500 or a rolled-back deliver, and never blocks the other.
    """
    home_url = request.build_absolute_uri(reverse("meso:athlete_home"))

    def _send():
        try:
            send_week_delivered_email(
                athlete=plan.athlete,
                coach=plan.coach,
                plan=plan,
                week=week,
                home_url=home_url,
            )
        except Exception:  # mail is best-effort; never fail a delivery on it
            logger.exception(
                "Failed to send delivery email for plan %s week %s",
                plan.pk,
                week.pk,
            )
        try:
            meso_push.notify_week_delivered(
                athlete=plan.athlete,
                coach=plan.coach,
                plan=plan,
                week=week,
                home_url=home_url,
            )
        except Exception:  # push is best-effort too; never fail a delivery on it
            logger.exception(
                "Failed to send delivery push for plan %s week %s",
                plan.pk,
                week.pk,
            )

    transaction.on_commit(_send)


# -- agent proposal engine (agent slice Phase 1 / Phase 4 — B6) -----------
#
# Runs the Claude proposal engine for an owned plan and persists a reviewable
# batch (the coach still approves at the review gate). Phase 4 runs it off the
# request thread: the endpoint creates a ``drafting`` batch, dispatches the job,
# and returns 202 + a ``status_url``; the frontend polls ``batch_status`` until
# the batch resolves to ``pending`` (changes + review link) or ``failed`` (with
# the reason). Returns 503 — before creating a batch — when no API key is
# configured, so the feature degrades cleanly in envs without creds.

MAX_INSTRUCTION_LENGTH = 2000


@login_required
@require_POST
def agent_propose(request, plan_id):
    """Kick off an agent run for a plan and return a drafting batch to poll."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    if plan.is_group:
        # The agent grounds on a single athlete's profile + logs; a group agent
        # (per-athlete auto-adjusts) is groups Phase 3. Reject before any work so
        # it never dereferences ``plan.athlete``.
        return HttpResponseBadRequest("The agent isn't available for groups yet.")
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")
    instruction = payload.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return HttpResponseBadRequest("An instruction is required.")
    instruction = instruction.strip()
    if len(instruction) > MAX_INSTRUCTION_LENGTH:
        return HttpResponseBadRequest("Instruction is too long.")

    # Guard on the key here so we answer 503 without persisting a dead batch.
    if agent_client.get_default_client() is None:
        return JsonResponse(
            {"ok": False, "error": "The Meso agent is not configured (no API key)."},
            status=503,
        )

    batch = agent_service.create_drafting_batch(plan, instruction, coach=request.user)
    agent_jobs.dispatch_proposal(batch.pk)
    _touch_plan(plan)
    return JsonResponse(
        {
            "ok": True,
            "batch_id": batch.pk,
            "status": batch.status,
            "status_url": reverse(
                "meso:api_batch_status", kwargs={"batch_id": batch.pk}
            ),
        },
        status=202,
    )


@login_required
@require_GET
def batch_status(request, batch_id):
    """Poll a proposal batch's state while/after the background job runs.

    Scoped to a batch the requester coaches (404 otherwise). ``drafting`` while
    the job runs; ``pending`` with the serialized changes + a review link once it
    lands; ``failed`` with the reason when the provider/run failed.
    """
    batch = _coach_batch_or_404(request, batch_id)
    data = {"ok": True, "status": batch.status, "summary": batch.summary}
    if batch.status == AgentProposalBatch.Status.FAILED:
        data["error"] = batch.error
    elif batch.status != AgentProposalBatch.Status.DRAFTING:
        changes = [serialize_proposed_change(c) for c in batch.changes.all()]
        data["changes"] = changes
        if changes:
            data["review_url"] = reverse(
                "meso:review_batch", kwargs={"batch_id": batch.pk}
            )
    return JsonResponse(data)


# -- review gate: approve/reject + apply (agent slice Phase 2 — B6) --------
#
# The human gate is the review screen; these endpoints persist the coach's
# per-change decisions and then write the approved edits back into the program.
# Every action is scoped to a batch the requester coaches over an *active*
# relationship (``Plan.objects.for_coach``) — a foreign/unknown batch is a 404,
# never a silent write. Apply/dismiss only act on a still-``pending`` batch, so a
# double-submit is a clean 409 rather than a re-apply.


def _coach_batch_or_404(request, batch_id):
    """The batch the requester coaches, or raise ``Http404``."""
    batch = (
        AgentProposalBatch.objects.filter(
            pk=batch_id, plan__in=Plan.objects.for_coach(request.user)
        )
        .select_related("plan", "plan__relationship")
        .first()
    )
    if batch is None:
        raise Http404("Unknown proposal batch")
    return batch


@login_required
@require_POST
def change_set_status(request, pk):
    """Persist a coach's approve/reject decision on one proposed change."""
    change = (
        ProposedChange.objects.filter(
            pk=pk, batch__plan__in=Plan.objects.for_coach(request.user)
        )
        .select_related("batch")
        .first()
    )
    if change is None:
        raise Http404("Unknown proposed change")
    if change.batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")
    status = payload.get("status")
    allowed = {ProposedChange.Status.APPROVED, ProposedChange.Status.REJECTED}
    if status not in allowed:
        return HttpResponseBadRequest("status must be 'approved' or 'rejected'.")
    change.status = status
    change.save(update_fields=["status"])
    return JsonResponse({"ok": True, "id": change.pk, "status": change.status})


@login_required
@require_POST
def batch_apply(request, batch_id):
    """Apply the batch's approved changes back into the program."""
    batch = _coach_batch_or_404(request, batch_id)
    if batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    result = agent_apply.apply_batch(batch)
    return JsonResponse(
        {
            "ok": True,
            "applied": result["applied"],
            "skipped": result["skipped"],
            "deliver_url": reverse(
                "meso:deliver_plan", kwargs={"plan_id": batch.plan_id}
            ),
        }
    )


@login_required
@require_POST
def batch_dismiss(request, batch_id):
    """Discard a batch without applying anything."""
    batch = _coach_batch_or_404(request, batch_id)
    if batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    agent_apply.dismiss_batch(batch)
    return JsonResponse(
        {
            "ok": True,
            "designer_url": reverse(
                "meso:designer_plan", kwargs={"plan_id": batch.plan_id}
            ),
        }
    )


# -- still on fixtures until their own slices ------------------------------


class ChangeReviewView(LoginRequiredMixin, TemplateView):
    """Review the batch of edits the agent proposes before they hit the program.

    ``review/<batch_id>/`` renders a real, owned ``AgentProposalBatch``; the coach
    approves/rejects per change and applies the batch (Phase 2). The bare
    ``review/`` redirects to the coach's latest pending batch (fixtures retired).
    """

    template_name = "meso/review.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("batch_id") is None:
            # The latest pending batch across *any* plan the coach owns — a
            # proposal on one athlete shouldn't be missed because another is the
            # working plan.
            batch = (
                AgentProposalBatch.objects.filter(
                    plan__in=Plan.objects.for_coach(request.user),
                    status=AgentProposalBatch.Status.PENDING,
                )
                .order_by("-created_at")
                .first()
            )
            if batch is None:
                messages.info(request, "No proposals to review yet.")
                return redirect("meso:designer")
            return redirect("meso:review_batch", batch_id=batch.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        batch = (
            AgentProposalBatch.objects.filter(
                pk=kwargs["batch_id"],
                plan__in=Plan.objects.for_coach(self.request.user),
            )
            .select_related("plan", "plan__relationship__athlete")
            .first()
        )
        if batch is None:
            raise Http404("Unknown proposal batch")
        ctx.update(presenters.review_changes(batch))
        ctx["batch_id"] = batch.pk
        ctx["plan_id"] = batch.plan_id
        ctx["is_pending"] = batch.status == AgentProposalBatch.Status.PENDING
        return ctx


class DeliverView(LoginRequiredMixin, TemplateView):
    """Confirm what gets sent to the athlete, when, and how.

    The screen binds to a real, owned plan: it shows that plan's athlete +
    current week and its "Deliver" button POSTs to ``plan_deliver`` (stamp +
    snapshot). The bare URL redirects to the coach's working plan (or the
    roster) now that the prototype fixtures are retired (Phase 5).
    """

    template_name = "meso/deliver.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("plan_id") is None:
            plan = _coach_working_plan(request.user)
            if plan is None:
                messages.info(request, "Pick an athlete to deliver a program.")
                return redirect("meso:roster")
            return redirect("meso:deliver_plan", plan_id=plan.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        plan = (
            Plan.objects.for_coach(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        ctx["plan_id"] = plan.pk
        ctx.update(presenters.deliver_screen(plan))
        return ctx


class ResultsView(LoginRequiredMixin, TemplateView):
    """Logged session results vs targets — closes the loop back to the agent.

    Binds to a real, owned session (``results/<session_id>/``): the athlete's
    logged sets scored against the prescribed grid (athlete slice Phase 3, the
    coach-side fixtures retired). The bare ``results/`` redirects to the coach's
    most-recently-logged session, or back to the roster if none — mirroring the
    designer/deliver bare redirects.
    """

    template_name = "meso/results.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("session_id") is None:
            session = _coach_latest_logged_session(request.user)
            if session is None:
                messages.info(request, "No logged sessions yet.")
                return redirect("meso:roster")
            return redirect("meso:results_session", session_id=session.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = _coach_session_or_404(self.request.user, kwargs["session_id"])
        ctx["active"] = "roster"
        ctx.update(presenters.session_results(session))
        return ctx
