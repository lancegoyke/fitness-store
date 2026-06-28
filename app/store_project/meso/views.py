import datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from . import mockdata
from . import presenters
from .agent import apply as agent_apply
from .agent import client as agent_client
from .agent import jobs as agent_jobs
from .agent import service as agent_service
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachProfile
from .models import ExercisePrescription
from .models import LoggedSet
from .models import Plan
from .models import ProposedChange
from .models import Session
from .models import SessionLog
from .models import WeekDelivery
from .serializers import current_week
from .serializers import serialize_plan
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change
from .serializers import serialize_session_log
from .serializers import serialize_week_snapshot


def _coach_working_plan(user):
    """The coach's most-recently-touched, non-archived plan, or None.

    The target the bare ``/meso/designer/`` and ``/meso/deliver/`` URLs resolve
    to now that the client-side fixtures are retired (Phase 5): a coach lands on
    the plan they last worked, or back on the roster if they have none.
    """
    return (
        Plan.objects.for_coach(user)
        .exclude(status=Plan.Status.ARCHIVED)
        .order_by("-modified")
        .first()
    )


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso strength-training program designer.

    A self-contained, full-screen coach tool. The view serializes a real, owned
    plan into the page and the Alpine front-end hydrates from it (then autosaves
    edits to the API endpoints below). The bare URL has no fixtures anymore — it
    redirects to the coach's working plan (or the roster). The agent column
    stays mock until its slice.
    """

    template_name = "meso/designer.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("plan_id") is None:
            plan = _coach_working_plan(request.user)
            if plan is None:
                messages.info(request, "Pick an athlete to start a program.")
                return redirect("meso:roster")
            return redirect("meso:designer_plan", plan_id=plan.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        plan = (
            Plan.objects.for_coach(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        ctx["plan_data"] = serialize_plan(plan)
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
        ctx["active"] = "roster"
        ctx["athletes"] = athletes
        # Groups (S1) need shared programs; activity (Phase 3) needs logged
        # sessions; needs-review (Phase 2) needs agent state. Empty until then.
        ctx["groups"] = []
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


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's own logged-in surface, distinct from the coach's view of an
# athlete (``/meso/athlete/<uuid>/``). Read-only here; logging lands in Phase 2.
# Everything is scoped to the athlete's *active* coaches (``for_athlete``), to
# **delivered** weeks (delivery gates *visibility* — an undelivered week is
# hidden; a delivered week's *current* contents are shown, see
# ``latest_delivered_week``), and to non-archived plans. An out-of-scope session
# is a flat 404 — never a silent empty render.


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

    raw_date = payload.get("date")
    if raw_date in (None, ""):
        log_date = timezone.localdate()
    elif isinstance(raw_date, str):
        try:
            log_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return HttpResponseBadRequest("date must be an ISO date (YYYY-MM-DD).")
    else:
        return HttpResponseBadRequest("date must be an ISO date string.")

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
        log.date = log_date
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

    404 when the plan does not exist; 403 when it exists but the requester is
    not its coach over an active relationship.
    """
    plan = get_object_or_404(Plan, pk=plan_id)
    if plan.relationship.coach_id != request.user.id or not plan.relationship.is_active:
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
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "week": {"id": week.pk, "label": f"Wk {week.index}"},
        },
        status=201,
    )


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
    """Logged session results vs targets — closes the loop back to the agent."""

    template_name = "meso/results.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "roster"
        ctx["summary"] = mockdata.RESULTS_SUMMARY
        ctx["rows"] = mockdata.RESULTS_ROWS
        ctx["athlete"] = mockdata.athlete_by_slug(mockdata.RESULTS_SUMMARY["athlete"])
        return ctx
