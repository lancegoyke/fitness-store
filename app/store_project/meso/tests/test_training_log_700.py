"""#700 — the athlete's Training log.

"If it counts, you can see it": the log shows exactly the sets
``LoggedSet.objects.performance_history`` counts, whatever became of their day,
week, exercise row, plan or coach. It is built from ``SessionLog`` /
``LoggedSet`` alone and is read-only; the athlete's PR panel links into it.
"""

import datetime
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

LOG_URL = "meso:athlete_log"
WORKOUT_URL = "meso:athlete_workout"
HOME_URL = "meso:athlete_home"

# Pinned query counts for the page (client session + user lookup included); the
# list is the same for 3 logs and for 9.
LIST_QUERIES = 7
WORKOUT_QUERIES = 6


def today():
    return timezone.localdate()


def world(
    athlete,
    coach=None,
    *,
    unit=Unit.KILOGRAMS,
    day_name="Lower",
    lift="Back Squat",
    title=None,
):
    """One active coach link, plan, block, week 1, day 1 and one exercise row."""
    coach = coach or UserFactory()
    link = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan_kwargs = {"title": title} if title else {}
    plan = PlanFactory(
        relationship=link, status=Plan.Status.ACTIVE, unit=unit, **plan_kwargs
    )
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    week = WeekFactory(mesocycle=meso, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name=day_name)
    cell = presc(session, name=lift, order=0, text="3 x 5")
    return _Ns(
        coach=coach,
        athlete=athlete,
        link=link,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        cell=cell,
    )


class _Ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def add_day(w, n, *, name, lift="Back Squat"):
    """Another day of ``w``'s week with one exercise row; returns (session, cell)."""
    session = day(w.week, day_number=n, name=name)
    return session, presc(session, name=lift, order=0, text="3 x 5")


def make_log(athlete, session, *, date=None, status=SessionLog.Status.DONE, notes=""):
    return SessionLog.objects.create(
        session=session, athlete=athlete, date=date, status=status, notes=notes
    )


def lset(log, cell, n, *, load="140", reps="5", unit="kg", **kw):
    return LoggedSet.objects.create(
        session_log=log,
        prescription=cell,
        set_number=n,
        reps=reps,
        load=load,
        unit=unit,
        **kw,
    )


def html(response):
    return response.content.decode()


def entries(body):
    return re.findall(r'data-testid="training-log-entry" href="([^"]+)"', body)


def logged_set_ids(body):
    return {int(i) for i in re.findall(r'data-logged-set="(\d+)"', body)}


@pytest.fixture
def athlete():
    return UserFactory()


@pytest.fixture
def squat_log(athlete):
    """A DONE log of 3 back-squat sets (the 140 kg x 5 top) on a live day."""
    w = world(athlete)
    log = make_log(athlete, w.session, date=today() - datetime.timedelta(days=2))
    for n, load in enumerate(("100", "120", "140"), start=1):
        lset(log, w.cell, n, load=load)
    return _Ns(w=w, log=log)


# -- 1. nothing the coach deletes hides a counted set -----------------------


class TestDeletedStructureStaysVisible:
    def _check(self, client, athlete, log, *, name, label):
        client.force_login(athlete)
        listing = html(client.get(reverse(LOG_URL)))
        assert reverse(WORKOUT_URL, kwargs={"log_pk": log.pk}) in listing
        assert name in listing
        page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})))
        assert "Back Squat" in page
        assert label in page

    def test_set_on_a_soft_deleted_day_still_shows(self, client, athlete, squat_log):
        squat_log.w.session.session_slot.soft_delete()
        self._check(
            client, athlete, squat_log.log, name="Lower", label="Set 3 · 140 kg × 5"
        )

    def test_set_on_a_soft_deleted_week_still_shows(self, client, athlete, squat_log):
        squat_log.w.week.soft_delete()
        self._check(
            client, athlete, squat_log.log, name="Lower", label="Set 3 · 140 kg × 5"
        )

    def test_set_on_a_soft_deleted_exercise_row_still_shows(
        self, client, athlete, squat_log
    ):
        squat_log.w.cell.exercise_slot.soft_delete()
        self._check(
            client, athlete, squat_log.log, name="Lower", label="Set 3 · 140 kg × 5"
        )


# -- 2. an ended coach does not take the history with them ------------------


def test_ended_coach_keeps_log_workout_and_home_records(client, athlete, squat_log):
    w = squat_log.w
    w.link.end(by="coach")
    client.force_login(athlete)

    listing = html(client.get(reverse(LOG_URL)))
    assert reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk}) in listing
    assert f"Coach {w.coach.name}" in listing or "Coach " in listing

    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk})))
    assert 'data-testid="open-session"' not in page
    assert 'data-testid="workout-readonly-note"' in page
    assert "Coach " in page

    home = html(client.get(reverse(HOME_URL)))
    assert 'data-testid="training-log-link"' in home
    assert "Personal records" in home
    assert "Back Squat" in home


# -- 3. the coach is named only when it disambiguates -----------------------


def test_two_coaches_each_label_their_workouts(client, athlete):
    a = world(athlete, UserFactory(name="Alice Coach"), day_name="Upper A")
    b = world(athlete, UserFactory(name="Bob Coach"), day_name="Upper B")
    make_log(athlete, a.session, date=today())
    make_log(athlete, b.session, date=today())
    for ns in (a, b):
        lset(SessionLog.objects.get(session=ns.session), ns.cell, 1)
    client.force_login(athlete)
    body = html(client.get(reverse(LOG_URL)))
    assert "Coach Alice Coach" in body
    assert "Coach Bob Coach" in body


def test_one_active_coach_shows_no_coach_label(client, athlete, squat_log):
    client.force_login(athlete)
    assert "Coach " not in html(client.get(reverse(LOG_URL)))


# -- 4. a PR links to the workout that set it -------------------------------


def test_pr_provenance_links_to_the_log_holding_the_best_set(client, athlete):
    w = world(athlete)
    w2 = WeekFactory(mesocycle=w.meso, index=2, delivered_at=timezone.now())
    s2 = day(w2, session_slot=w.session.session_slot)
    cell2 = presc(exercise_slot=w.cell.exercise_slot, week=w2, text="3 x 5")
    light = make_log(athlete, w.session, date=today() - datetime.timedelta(days=9))
    lset(light, w.cell, 1, load="100")
    heavy = make_log(athlete, s2, date=today() - datetime.timedelta(days=2))
    lset(heavy, cell2, 1, load="150")

    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    href = reverse(WORKOUT_URL, kwargs={"log_pk": heavy.pk})
    assert f'<a href="{href}" data-testid="pr-provenance"' in body

    # The coach's own panel never links into an athlete-only URL.
    client.force_login(w.coach)
    profile = html(client.get(reverse("meso:athlete", kwargs={"pk": athlete.pk})))
    assert "from 5×150" in profile
    assert 'data-testid="pr-provenance"' not in profile
    assert "/me/log/" not in profile


# -- 5. nothing leaks across athletes ---------------------------------------


def test_another_athletes_log_is_a_404_and_leaks_nothing(client, squat_log):
    client.force_login(UserFactory())
    resp = client.get(reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk}))
    assert resp.status_code == 404
    body = html(resp)
    assert "Lower" not in body
    assert "140" not in body


def test_unknown_log_is_a_404(client, athlete):
    client.force_login(athlete)
    assert (
        client.get(reverse(WORKOUT_URL, kwargs={"log_pk": 999999})).status_code == 404
    )


def test_anonymous_is_sent_to_login(client, squat_log):
    for url in (
        reverse(LOG_URL),
        reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk}),
    ):
        resp = client.get(url)
        assert resp.status_code == 302
        assert "login" in resp["Location"]


# -- 6. notes ----------------------------------------------------------------


def test_notes_only_log_appears_and_shows_its_notes(client, athlete):
    w = world(athlete)
    log = make_log(athlete, w.session, date=today(), notes="Felt strong")
    client.force_login(athlete)
    listing = html(client.get(reverse(LOG_URL)))
    assert reverse(WORKOUT_URL, kwargs={"log_pk": log.pk}) in listing
    assert "Felt strong" in listing
    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})))
    assert 'data-testid="workout-notes"' in page
    assert "Felt strong" in page
    assert 'data-testid="workout-exercise"' not in page


@pytest.mark.parametrize("notes", ["", "  \n\t "])
def test_log_with_no_sets_and_blank_notes_is_hidden(client, athlete, notes):
    w = world(athlete)
    log = make_log(athlete, w.session, date=today(), notes=notes)
    client.force_login(athlete)
    assert entries(html(client.get(reverse(LOG_URL)))) == []
    assert (
        client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})).status_code == 404
    )


# -- 7. query counts ---------------------------------------------------------


def _many_logs(athlete, n):
    """``n`` logs over two coaches, two exercises x three sets each."""
    a = world(athlete, day_name="Day 1")
    b = world(athlete, day_name="Day 1")
    for i in range(n):
        owner = a if i % 2 == 0 else b
        session, cell = add_day(owner, 10 + i, name=f"Extra {i}", lift="Bench Press")
        cell2 = presc(session, name="Row", order=1, text="3 x 5")
        log = make_log(athlete, session, date=today() - datetime.timedelta(days=i))
        for k in (1, 2, 3):
            lset(log, cell, k)
            lset(log, cell2, k, load="60")
    return log


def test_list_page_query_count_is_flat(client, athlete, django_assert_num_queries):
    _many_logs(athlete, 3)
    client.force_login(athlete)
    with django_assert_num_queries(LIST_QUERIES):
        assert client.get(reverse(LOG_URL)).status_code == 200


def test_list_page_query_count_does_not_grow_with_logs(
    client, athlete, django_assert_num_queries
):
    _many_logs(athlete, 9)
    client.force_login(athlete)
    with django_assert_num_queries(LIST_QUERIES):
        assert client.get(reverse(LOG_URL)).status_code == 200


def test_workout_page_query_count_is_pinned(client, athlete, django_assert_num_queries):
    log = _many_logs(athlete, 3)
    client.force_login(athlete)
    with django_assert_num_queries(WORKOUT_QUERIES):
        assert (
            client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})).status_code
            == 200
        )


# -- 8. set-coverage invariant -----------------------------------------------


def test_every_counted_set_is_reachable_from_the_log(client, athlete):
    live = world(athlete, day_name="Live", lift="Back Squat")
    ended = world(athlete, day_name="Archived", lift="Deadlift")
    other = world(athlete, UserFactory(), day_name="Other coach", lift="Press")

    def logged(owner, session, cell, **kw):
        log = make_log(athlete, session, date=today(), **kw)
        lset(log, cell, 1)
        lset(log, cell, 2, load="150")
        return log

    logged(live, live.session, live.cell)
    # A deleted day, week and exercise row.
    s, c = add_day(live, 2, name="Deleted day")
    logged(live, s, c)
    s.session_slot.soft_delete()
    w2 = WeekFactory(mesocycle=live.meso, index=2, delivered_at=timezone.now())
    s2 = day(w2, day_number=3, name="Deleted week")
    c2 = presc(s2, name="Curl", order=0, text="3 x 5")
    logged(live, s2, c2)
    w2.soft_delete()
    s3, c3 = add_day(live, 4, name="Deleted row", lift="Lunge")
    logged(live, s3, c3)
    c3.exercise_slot.soft_delete()
    # A skipped cell, a PENDING log, a renamed lift, a coach-entered set.
    s4 = day(live.week, day_number=5, name="Skipped")
    c4 = presc(s4, name="Skipped lift", order=0, text="3 x 5", skipped=True)
    logged(live, s4, c4)
    s5, c5 = add_day(live, 6, name="Pending", lift="Dip")
    p = make_log(athlete, s5, date=today(), status=SessionLog.Status.PENDING)
    lset(p, c5, 1)
    s6, c6 = add_day(live, 7, name="Renamed", lift="Old name")
    r = logged(live, s6, c6)
    c6.exercise_slot.name = "New name"
    c6.exercise_slot.save()
    s7, c7 = add_day(live, 8, name="Coach entered", lift="Shrug")
    ce = make_log(athlete, s7, date=today())
    lset(ce, c7, 1, entered_by_coach=True)
    assert r.pk
    # An archived plan of an ended coach, and a second coach.
    logged(ended, ended.session, ended.cell)
    ended.link.end(by="coach")
    logged(other, other.session, other.cell)
    # Enough logs to need a second page.
    for i in range(22):
        session, cell = add_day(live, 20 + i, name=f"Filler {i}")
        lset(
            make_log(athlete, session, date=today() - datetime.timedelta(days=i + 1)),
            cell,
            1,
        )

    client.force_login(athlete)
    url, seen_logs = reverse(LOG_URL), []
    while url:
        body = html(
            client.get(url if url.startswith("/") else f"{reverse(LOG_URL)}{url}")
        )
        seen_logs += entries(body)
        url = (
            re.search(r'data-testid="training-log-older" href="([^"]+)"', body)
            or [None, ""]
        )[1]
    assert len(seen_logs) == len(set(seen_logs)) > 20

    collected = set()
    for workout in seen_logs:
        collected |= logged_set_ids(html(client.get(workout)))
    counted = set(
        LoggedSet.objects.performance_history(athlete).values_list("pk", flat=True)
    )
    assert collected == counted


# -- 9. pagination ------------------------------------------------------------


def _logs(athlete, n):
    w = world(athlete)
    for i in range(n):
        s, c = add_day(w, 10 + i, name=f"Day {i}")
        lset(make_log(athlete, s, date=today() - datetime.timedelta(days=i)), c, 1)
    return w


def test_pagination_splits_at_twenty(client, athlete):
    _logs(athlete, 21)
    client.force_login(athlete)
    first = html(client.get(reverse(LOG_URL)))
    assert len(entries(first)) == 20
    assert 'data-testid="training-log-older"' in first
    assert 'data-testid="training-log-newer"' not in first
    second = html(client.get(reverse(LOG_URL), {"page": 2}))
    assert len(entries(second)) == 1
    assert 'data-testid="training-log-newer" href="?page=1"' in second
    assert 'data-testid="training-log-older"' not in second


def test_garbage_page_renders_the_first_page(client, athlete):
    _logs(athlete, 21)
    client.force_login(athlete)
    body = html(client.get(reverse(LOG_URL), {"page": "x"}))
    assert len(entries(body)) == 20
    assert 'data-testid="training-log-older"' in body


# -- 10. date labels -----------------------------------------------------------


def test_today_and_yesterday_labels(client, athlete):
    w = world(athlete)
    s2, c2 = add_day(w, 2, name="Yday")
    lset(make_log(athlete, w.session, date=today()), w.cell, 1)
    lset(make_log(athlete, s2, date=today() - datetime.timedelta(days=1)), c2, 1)
    client.force_login(athlete)
    body = html(client.get(reverse(LOG_URL)))
    assert ">Today<" in body
    assert ">Yesterday<" in body


def test_day_label_formats_this_year_and_other_years():
    now = datetime.date(2026, 10, 3)
    assert presenters._log_day_label(datetime.date(2026, 9, 17), now) == "Thu, Sep 17"
    assert (
        presenters._log_day_label(datetime.date(2025, 9, 17), now)
        == "Wed, Sep 17, 2025"
    )


def test_undated_log_falls_back_to_its_creation_day(client, athlete):
    w = world(athlete)
    log = make_log(athlete, w.session, date=None)
    lset(log, w.cell, 1)
    client.force_login(athlete)
    assert ">Today<" in html(client.get(reverse(LOG_URL)))


def test_newest_first_is_by_training_date_not_creation_order(client, athlete):
    w = world(athlete, day_name="Recent")
    s2, c2 = add_day(w, 2, name="Backfilled")
    lset(
        make_log(athlete, w.session, date=today() - datetime.timedelta(days=1)),
        w.cell,
        1,
    )
    lset(make_log(athlete, s2, date=today() - datetime.timedelta(days=10)), c2, 1)
    client.force_login(athlete)
    body = html(client.get(reverse(LOG_URL)))
    assert body.index("Recent") < body.index("Backfilled")


# -- 11. in progress -----------------------------------------------------------


def test_pending_log_is_marked_in_progress_done_is_not(client, athlete):
    w = world(athlete)
    s2, c2 = add_day(w, 2, name="Finished")
    pending = make_log(
        athlete, w.session, date=today(), status=SessionLog.Status.PENDING
    )
    lset(pending, w.cell, 1)
    done = make_log(athlete, s2, date=today())
    lset(done, c2, 1)
    client.force_login(athlete)
    assert html(client.get(reverse(LOG_URL))).count('data-testid="in-progress"') == 1
    assert 'data-testid="in-progress"' in html(
        client.get(reverse(WORKOUT_URL, kwargs={"log_pk": pending.pk}))
    )
    assert 'data-testid="in-progress"' not in html(
        client.get(reverse(WORKOUT_URL, kwargs={"log_pk": done.pk}))
    )


# -- 12-14. the workout page ---------------------------------------------------


def test_renamed_lift_keeps_the_name_it_was_logged_as(client, athlete, squat_log):
    slot = squat_log.w.cell.exercise_slot
    slot.name = "Front Squat"
    slot.save()
    client.force_login(athlete)
    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk})))
    assert "Back Squat" in page
    assert "Front Squat" not in page


def test_unanchored_legacy_sets_still_show(client, athlete):
    # ``performance_history`` doesn't require an anchor, so neither does the log:
    # a stamped set lost both pointers keeps its lift name, an unstamped one
    # still shows, and neither drops off the page.
    w = world(athlete)
    log = make_log(athlete, w.session, date=today())
    lset(log, w.cell, 1)
    stamped = LoggedSet.objects.create(
        session_log=log, set_number=2, reps="8", load="40", exercise_name="Carry"
    )
    bare = LoggedSet.objects.create(session_log=log, set_number=3, reps="10")
    assert stamped.anchor_slot_id is None and bare.anchor_slot_id is None
    client.force_login(athlete)
    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})))
    assert {stamped.pk, bare.pk} <= logged_set_ids(page)
    assert "Carry" in page
    assert "Unnamed exercise" in page


def test_coach_entered_set_says_so_athlete_set_does_not(client, athlete):
    w = world(athlete)
    log = make_log(athlete, w.session, date=today())
    mine = lset(log, w.cell, 1)
    theirs = lset(log, w.cell, 2, entered_by_coach=True)
    client.force_login(athlete)
    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": log.pk})))
    assert page.count("logged by coach") == 1
    assert re.search(
        rf'data-logged-set="{theirs.pk}"[^>]*>[^<]*<span[^>]*> · logged by coach', page
    )
    assert not re.search(rf'data-logged-set="{mine.pk}"[^>]*>[^<]*<span', page)


def test_reachable_session_offers_open_session(client, athlete, squat_log):
    client.force_login(athlete)
    page = html(client.get(reverse(WORKOUT_URL, kwargs={"log_pk": squat_log.log.pk})))
    href = reverse("meso:athlete_session", kwargs={"pk": squat_log.w.session.pk})
    assert f'data-testid="open-session" href="{href}"' in page
    assert 'data-testid="workout-readonly-note"' not in page


# -- 15. home -------------------------------------------------------------------


def test_home_links_the_log_for_an_athlete_with_nothing(client, athlete):
    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    assert 'data-testid="training-log-link"' in body
    assert "No active programs yet" in body
    assert "Your program, live as your coach writes it" not in body


def test_home_points_an_ended_athlete_at_their_past_workouts(
    client, athlete, squat_log
):
    squat_log.w.link.end(by="coach")
    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    assert "Your past workouts are still in your" in body
    assert "No active programs yet" not in body


# -- 16. the PR unit follows the history, not the preference --------------------


def test_archived_lb_plan_records_show_despite_a_kg_preference(client, athlete):
    w = world(athlete, unit=Unit.POUNDS)
    log = make_log(athlete, w.session, date=today() - datetime.timedelta(days=3))
    lset(log, w.cell, 1, load="225", unit="lb")
    w.link.end(by="coach")
    AthleteProfile.objects.update_or_create(
        user=athlete, defaults={"unit": Unit.KILOGRAMS}
    )
    CoachProfile.objects.update_or_create(
        user=w.coach, defaults={"default_unit": Unit.KILOGRAMS}
    )
    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    assert "Personal records" in body
    assert re.search(r"\d+ lb</span>", body)


def test_pr_unit_follows_the_newest_training_date_not_the_last_created(client, athlete):
    lb = world(athlete, unit=Unit.POUNDS, lift="Lb Lift")
    kg = world(athlete, unit=Unit.KILOGRAMS, lift="Kg Lift")
    lset(
        make_log(athlete, lb.session, date=today() - datetime.timedelta(days=10)),
        lb.cell,
        1,
        load="225",
        unit="lb",
    )
    # Created later, but backfilled with an older training date.
    lset(
        make_log(athlete, kg.session, date=today() - datetime.timedelta(days=60)),
        kg.cell,
        1,
        load="100",
        unit="kg",
    )
    lb.link.end(by="coach")
    kg.link.end(by="coach")
    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    assert "Lb Lift" in body
    assert "Kg Lift" not in body


def test_a_newest_unit_with_no_record_gives_way_to_one_that_has_records(
    client, athlete
):
    # The newest counted set is a bodyweight pull-up on a kg plan: it counts, but
    # Epley can't size it, so kg has no record. The older lb squat does.
    lb = world(athlete, unit=Unit.POUNDS, lift="Back Squat")
    kg = world(athlete, unit=Unit.KILOGRAMS, lift="Pull-up")
    lset(
        make_log(athlete, lb.session, date=today() - datetime.timedelta(days=9)),
        lb.cell,
        1,
        load="225",
        unit="lb",
    )
    lset(
        make_log(athlete, kg.session, date=today() - datetime.timedelta(days=2)),
        kg.cell,
        1,
        load="BW",
        reps="8",
    )
    lb.link.end(by="coach")
    kg.link.end(by="coach")
    client.force_login(athlete)
    body = html(client.get(reverse(HOME_URL)))
    assert "Back Squat" in body
    assert re.search(r"\d+ lb</span>", body)


def test_service_worker_never_stores_the_training_log(client):
    # The log is an online page: the worker sends it to the network only and
    # falls back to the offline page, never a stored copy that a second
    # athlete on the same phone could reopen. Behaviour is driven in the e2e
    # suite; this pins the wiring.
    body = client.get(reverse("meso:service_worker")).content.decode()
    assert f'const LOG_URL = "{reverse(LOG_URL)}"' in body
    assert "startsWith(LOG_URL)" in body


# -- 17. the summary line --------------------------------------------------------


def test_summary_reads_sets_and_top_set(client, athlete, squat_log):
    client.force_login(athlete)
    assert "Back Squat 3 sets, top 140 kg × 5" in html(client.get(reverse(LOG_URL)))


def test_single_set_summary_has_no_top_word(client, athlete):
    w = world(athlete)
    lset(make_log(athlete, w.session, date=today()), w.cell, 1)
    client.force_login(athlete)
    body = html(client.get(reverse(LOG_URL)))
    assert "Back Squat 1 set, 140 kg × 5" in body
    assert ", top " not in body
