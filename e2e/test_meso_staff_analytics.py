"""Staff opens the product-analytics dashboard and switches its window (#509).

Desktop only: `/meso/analytics/` is a staff-only, wide-table dashboard, never
opened on a phone — same reasoning as `test_meso_coach_agent.py`'s
`@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)`.

`analytics_data` below seeds one realistic week of Meso activity so all four
of `product_analytics.html`'s sections (Active users, Activation funnel,
Feature adoption, Email) read non-zero: Casey Coach invites Alex Athlete by
email, Alex accepts, Casey delivers the first block, and Alex logs the first
set — the full email-invite activation path (`docs/meso/decisions.md`
"Product analytics dashboard (#509)") — plus a push subscription and the two
Meso-authored emails (block-delivered, coach-invite) that path sends. Jordan
Coach separately edits a plan, runs the agent, and imports a template — the
other feature-adoption rows — and has one more invite still `pending`, sent
20 days ago: the only stale artifact here, inside the 30-day window but
outside the 7-day one, so switching the window visibly drops the activation
funnel's "Sent" count.
"""

import datetime
import re

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.analytics.events import EventName
from store_project.analytics.track import track
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.history import record_plan_action
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import PushSubscription
from store_project.meso.models import SessionLog
from store_project.meso.models import WeekDelivery
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def analytics_data(db):
    """Seed two coaches' worth of real activity — see the module docstring.

    Every `auto_now_add` timestamp is backdated with a queryset `.update()`
    after creation (the app's own state machines always stamp "now"; this
    moves rows into the past so the dashboard's 7/30-day windows disagree).
    """
    now = timezone.now()

    def ago(days):
        return now - datetime.timedelta(days=days)

    staff = UserFactory(is_staff=True, name="Sam Staff", email="sam.staff@example.com")
    # A CoachProfile just lands staff on the (empty, harmless) roster at
    # `/meso/` instead of being bounced to the athlete home — `_ineligible_
    # users()` excludes staff from every count below regardless.
    CoachProfileFactory(user=staff)

    # -- Casey Coach + Alex Athlete: the full email-invite activation path --
    casey = UserFactory(name="Casey Coach", email="casey.coach@example.com")
    alex = UserFactory(name="Alex Athlete", email="alex.athlete@example.com")
    CoachProfileFactory(user=casey)

    invite = CoachInviteFactory(coach=casey, email=alex.email)
    link = invite.accept(alex)
    CoachInvite.objects.filter(pk=invite.pk).update(
        created_at=ago(5), responded_at=ago(4)
    )
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=ago(5), responded_at=ago(4)
    )

    plan = PlanFactory(
        relationship=link, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    Plan.objects.filter(pk=plan.pk).update(created=ago(4))
    track(EventName.PLAN_CREATED, actor=casey, subject=plan, athlete=str(alex.pk))

    mesocycle = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=mesocycle, index=1, delivered_at=ago(3))
    session = day(week, day_number=1, name="Lower", bias="Squat")
    squat = presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    presc(
        session,
        name="Romanian Deadlift",
        order=1,
        sets="3",
        reps="8",
        load="80",
        rpe="8",
    )

    with transaction.atomic():
        record_plan_action(plan, "Edited Box Squat")
    PlanAction.objects.filter(plan=plan).update(created_at=ago(4))

    delivery = WeekDelivery.objects.create(week=week, delivered_at=ago(3))
    WeekDelivery.objects.filter(pk=delivery.pk).update(created_at=ago(3))

    log = SessionLogFactory(
        session=session, athlete=alex, status=SessionLog.Status.DONE
    )
    LoggedSetFactory(
        session_log=log, prescription=squat, set_number=1, reps="5", load="100", rpe="8"
    )
    SessionLog.objects.filter(pk=log.pk).update(created_at=ago(2))
    track(EventName.SESSION_COMPLETED, actor=alex, subject=log)

    push = PushSubscription.objects.create(
        athlete=alex,
        endpoint="https://fcm.googleapis.com/fcm/send/e2e-analytics-alex",
        p256dh="p256dh-test-key",
        auth="auth-test-secret",
    )
    PushSubscription.objects.filter(pk=push.pk).update(created_at=ago(2))

    sent_block = SentEmail.objects.create(
        ses_message_id="e2e-analytics-block-delivered",
        kind=EmailKind.BLOCK_DELIVERED,
        recipient=alex.email,
        user=alex,
        subject="Your week is ready",
        sent_at=ago(3),
    )
    EmailEvent.objects.create(
        sent_email=sent_block,
        event_type=EmailEvent.EventType.DELIVERY,
        ses_message_id=sent_block.ses_message_id,
        sns_message_id="e2e-analytics-block-delivered-delivery",
        recipient=alex.email,
        kind=EmailKind.BLOCK_DELIVERED,
        occurred_at=ago(3),
    )
    EmailEvent.objects.create(
        sent_email=sent_block,
        event_type=EmailEvent.EventType.OPEN,
        ses_message_id=sent_block.ses_message_id,
        sns_message_id="e2e-analytics-block-delivered-open",
        recipient=alex.email,
        kind=EmailKind.BLOCK_DELIVERED,
        occurred_at=ago(2),
    )

    sent_invite_email = SentEmail.objects.create(
        ses_message_id="e2e-analytics-coach-invite",
        kind=EmailKind.COACH_INVITE,
        recipient=alex.email,
        user=alex,
        subject="Casey invited you to train",
        sent_at=ago(5),
    )
    EmailEvent.objects.create(
        sent_email=sent_invite_email,
        event_type=EmailEvent.EventType.DELIVERY,
        ses_message_id=sent_invite_email.ses_message_id,
        sns_message_id="e2e-analytics-coach-invite-delivery",
        recipient=alex.email,
        kind=EmailKind.COACH_INVITE,
        occurred_at=ago(5),
    )
    EmailEvent.objects.create(
        sent_email=sent_invite_email,
        event_type=EmailEvent.EventType.CLICK,
        ses_message_id=sent_invite_email.ses_message_id,
        sns_message_id="e2e-analytics-coach-invite-click",
        recipient=alex.email,
        kind=EmailKind.COACH_INVITE,
        occurred_at=ago(4),
    )

    # -- Jordan Coach + Morgan Athlete: plan edits, an agent run, a template
    # import, and one invite still pending (the 20-day-old artifact) --
    jordan = UserFactory(name="Jordan Coach", email="jordan.coach@example.com")
    morgan = UserFactory(name="Morgan Athlete", email="morgan.athlete@example.com")
    CoachProfileFactory(user=jordan)

    rel2 = CoachAthleteFactory(coach=jordan, athlete=morgan)
    CoachAthlete.objects.filter(pk=rel2.pk).update(created_at=ago(6))

    plan2 = PlanFactory(
        relationship=rel2, title="Strength Block", status=Plan.Status.ACTIVE
    )
    Plan.objects.filter(pk=plan2.pk).update(created=ago(6))
    track(EventName.PLAN_CREATED, actor=jordan, subject=plan2, athlete=str(morgan.pk))
    track(EventName.TEMPLATE_IMPORTED, actor=jordan, subject=plan2)

    mesocycle2 = MesocycleFactory(plan=plan2, name="Block 1", order=0)
    week2 = WeekFactory(mesocycle=mesocycle2, index=1)
    session2 = day(week2, day_number=1, name="Push", bias="Press")
    presc(session2, name="Bench Press", order=0, sets="4", reps="6", load="80", rpe="7")

    with transaction.atomic():
        record_plan_action(plan2, "Edited Bench Press")
    PlanAction.objects.filter(plan=plan2).update(created_at=ago(6))

    batch = AgentProposalBatchFactory(
        plan=plan2,
        coach=jordan,
        status=AgentProposalBatch.Status.APPLIED,
        trigger=AgentProposalBatch.Trigger.MANUAL,
    )
    AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=ago(6))
    track(
        EventName.AGENT_PROPOSAL_RUN, actor=jordan, subject=batch, trigger=batch.trigger
    )
    track(EventName.BATCH_APPLIED, actor=jordan, subject=batch)

    # The one 20-day-old artifact: inside the 30-day window, outside the
    # 7-day one.
    pending_invite = CoachInviteFactory(
        coach=jordan, email="taylor.candidate@example.com"
    )
    CoachInvite.objects.filter(pk=pending_invite.pk).update(created_at=ago(20))

    return {
        "staff": staff,
        "casey": casey,
        "alex": alex,
        "jordan": jordan,
        "morgan": morgan,
    }


def _section(page, heading):
    """The `.meso-card` containing ``heading`` — scopes a locator to one card."""
    return page.locator(".meso-card").filter(
        has=page.get_by_role("heading", name=heading, exact=True)
    )


def _row(section, text):
    """The `<tr>` inside ``section`` whose text contains ``text``."""
    return section.get_by_role("row").filter(has_text=text)


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_staff_opens_analytics_and_switches_the_window(
    page, viewport, shot, login, analytics_data
):
    login(analytics_data["staff"])

    # Reach the dashboard the real way: /meso/ (a staff CoachProfile lands
    # staff on the roster), then the shared topnav's own staff-only
    # "Analytics" link (_meso_base.html) — not a goto.
    page.goto(reverse("meso:roster"))
    page.get_by_role("link", name="Analytics").click()
    expect(page).to_have_url(re.compile(r"/meso/analytics/"))

    for heading in ("Active users", "Activation funnel", "Feature adoption", "Email"):
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()

    # -- 1. Active users: both coaches are active in the default 30-day window --
    active_users = _section(page, "Active users")
    coaches_row = _row(active_users, "Coaches")
    expect(coaches_row.locator("td").nth(3)).to_have_text("2")  # Window (30d)

    # -- 2. Activation funnel: Casey's invite sent, accepted, delivered, logged --
    funnel = _section(page, "Activation funnel")
    email_invite_row = _row(funnel, "Email invite")
    expect(email_invite_row.locator("td").nth(1)).to_have_text("2")  # Sent
    expect(email_invite_row.locator("td").nth(4)).to_have_text("1")  # Logged

    # -- 3. Feature adoption: Casey's delivered block --
    features = _section(page, "Feature adoption")
    block_delivered_row = _row(features, "Block delivered")
    expect(block_delivered_row.locator("td").nth(4)).to_have_text("1")  # Times

    # -- 4. Email: the block-delivered email Alex opened --
    email = _section(page, "Email")
    email_block_row = _row(email, "Block delivered")
    expect(email_block_row.locator("td").nth(3)).to_have_text("1")  # Opened

    shot("01-30-days")

    # Switch to the 7-day window: Jordan's 20-day-old invite drops out of the
    # funnel's cohort (sent in the window), so "Sent" falls from 2 to 1 —
    # Casey's invite (5 days ago) is the only one left inside 7 days.
    page.get_by_role("link", name="7 days").click()
    expect(page).to_have_url(re.compile(r"days=7"))
    expect(page.get_by_role("link", name="7 days")).to_have_attribute(
        "aria-current", "page"
    )

    funnel = _section(page, "Activation funnel")
    email_invite_row = _row(funnel, "Email invite")
    expect(email_invite_row.locator("td").nth(1)).to_have_text("1")  # Sent: 2 -> 1

    shot("02-7-days")
