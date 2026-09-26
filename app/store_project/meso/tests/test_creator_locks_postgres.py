"""PostgreSQL regressions for Plan/CoachAthlete creator locks (#596)."""

import re
import threading
import time

import pytest
from django.db import DatabaseError
from django.db import connection
from django.db import transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from store_project.meso import demo
from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import Plan
from store_project.meso.tests.test_batch_deliver import comp
from store_project.meso.tests.test_batch_deliver import seed_source
from store_project.meso.tests.test_template_plans import template_plan
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row locks are not observable on SQLite.",
    ),
]


def _set_short_lock_timeout(lock_timeout="750ms", *, deadlock_timeout=None):
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, false)", [lock_timeout])
        if deadlock_timeout is not None:
            cursor.execute(
                "SELECT set_config('deadlock_timeout', %s, false)",
                [deadlock_timeout],
            )


def _wait_until_a_backend_is_lock_blocked(timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
            (blocked,) = cursor.fetchone()
            if blocked:
                return True
            time.sleep(interval)
    return False


def _hold_rows(model, pks, locked, release, errors):
    try:
        with transaction.atomic():
            list(
                model.objects.select_for_update(no_key=True)
                .filter(pk__in=pks)
                .order_by("pk")
            )
            locked.set()
            assert release.wait(timeout=8)
    except Exception as exc:  # pragma: no cover - surfaced below
        errors.append(exc)
    finally:
        connection.close()


def _run_while_rows_are_held(model, pks, request_call):
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    request_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_rows,
        args=(model, pks, locked, release, holder_errors),
    )

    def run_request():
        try:
            _set_short_lock_timeout()
            result["response"] = request_call()
        except Exception as exc:  # pragma: no cover - surfaced below
            request_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_request)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    was_blocked = _wait_until_a_backend_is_lock_blocked()
    release.set()
    holder.join(timeout=10)
    worker.join(timeout=10)
    return was_blocked, holder_errors, request_errors, result, holder, worker


def _demo_tree(coach):
    athlete = UserFactory()
    relationship = CoachAthleteFactory(
        coach=coach,
        athlete=athlete,
        is_demo=True,
    )
    plan = PlanFactory(relationship=relationship)
    return athlete, relationship, plan


def _race_creator_against_clear_demo(
    monkeypatch,
    *,
    coach,
    demo_athlete,
    demo_relationship,
    demo_plan,
    request_call,
    pause_seam,
    creator_lock_timeout="5s",
):
    paused = threading.Event()
    release_clear = threading.Event()
    creator_backend_ready = threading.Event()
    clear_thread = []
    clear_errors = []
    creator_errors = []
    result = {}

    def pause_only_clear_thread():
        if clear_thread and threading.current_thread() is clear_thread[0]:
            paused.set()
            assert release_clear.wait(timeout=8)

    if pause_seam == "coach-lock":
        original_demo_athletes = demo._demo_athletes

        def pause_after_coach_lock(locked_coach):
            pause_only_clear_thread()
            return original_demo_athletes(locked_coach)

        monkeypatch.setattr(demo, "_demo_athletes", pause_after_coach_lock)
    elif pause_seam == "cascade-locks":
        original_lock_cascade_parents = demo.lock_cascade_parents

        def pause_after_cascade_locks(user_ids):
            locked = original_lock_cascade_parents(user_ids)
            pause_only_clear_thread()
            return locked

        monkeypatch.setattr(
            demo,
            "lock_cascade_parents",
            pause_after_cascade_locks,
        )
    else:  # pragma: no cover - test-helper misuse
        raise ValueError(f"Unknown clear_demo pause seam: {pause_seam}")

    def run_clear_demo():
        clear_thread.append(threading.current_thread())
        try:
            _set_short_lock_timeout("5s", deadlock_timeout="100ms")
            demo.clear_demo(coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            clear_errors.append(exc)
        finally:
            connection.close()

    def run_creator():
        try:
            _set_short_lock_timeout(
                creator_lock_timeout,
                deadlock_timeout="100ms",
            )
            creator_backend_ready.set()
            result["response"] = request_call()
        except Exception as exc:  # pragma: no cover - surfaced below
            creator_errors.append(exc)
        finally:
            connection.close()

    clearer = threading.Thread(target=run_clear_demo)
    creator = threading.Thread(target=run_creator)
    creator_started = False
    try:
        clearer.start()
        assert paused.wait(timeout=5), f"clear_demo never reached its {pause_seam} seam"
        creator.start()
        creator_started = True
        assert creator_backend_ready.wait(timeout=5), (
            "the creator thread never opened its PostgreSQL connection"
        )
        was_blocked = _wait_until_a_backend_is_lock_blocked()
    finally:
        release_clear.set()
        clearer.join(timeout=10)
        if creator_started:
            creator.join(timeout=10)

    assert not clearer.is_alive(), "clear_demo's thread never finished"
    assert not creator.is_alive(), "the creator's thread never finished"
    assert clear_errors == []
    assert creator_errors == []
    assert was_blocked, f"the creator never waited for clear_demo's {pause_seam} lock"
    assert not demo.has_demo(coach)
    assert not User.objects.filter(pk=demo_athlete.pk).exists()
    assert not CoachAthlete.objects.filter(pk=demo_relationship.pk).exists()
    assert not Plan.objects.filter(pk=demo_plan.pk).exists()
    assert not Plan.objects.filter(relationship_id=demo_relationship.pk).exists()
    return result["response"]


def test_template_use_waits_for_the_target_link_lock():
    coach = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    template, _ = template_plan(coach, title="Locked template")

    def post_template():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:template_use", kwargs={"plan_id": template.pk}),
            {"relationship": relationship.pk},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(CoachAthlete, [relationship.pk], post_template)
    )

    assert blocked, "template_use did not take the target CoachAthlete lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_template_use_survives_a_live_clear_demo(monkeypatch):
    coach = UserFactory()
    demo_athlete, demo_relationship, demo_plan = _demo_tree(coach)
    template, _ = template_plan(coach, title="Clear-race template")
    client = Client()
    client.force_login(coach)

    def post_template():
        return client.post(
            reverse("meso:template_use", kwargs={"plan_id": template.pk}),
            {"relationship": demo_relationship.pk},
        )

    response = _race_creator_against_clear_demo(
        monkeypatch,
        coach=coach,
        demo_athlete=demo_athlete,
        demo_relationship=demo_relationship,
        demo_plan=demo_plan,
        request_call=post_template,
        pause_seam="cascade-locks",
    )

    assert response.status_code == 302
    assert response.url == reverse("meso:template_library")
    assert Plan.objects.filter(pk=template.pk).exists()
    assert not Plan.objects.filter(relationship__coach=coach).exists()


def test_template_use_does_not_lock_the_athlete_user_row():
    """The link lock is `OF SELF`, so it never locks the athlete's User row.

    `select_related("athlete")` would join `users_user` and lock that row too —
    CoachAthlete then User, the inversion of a User-rooted cascade
    (`lock_cascade_parents` takes User first).
    """
    coach = UserFactory()
    athlete = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=athlete)
    template, _ = template_plan(coach, title="Joined-row template")

    def post_template():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:template_use", kwargs={"plan_id": template.pk}),
            {"relationship": relationship.pk},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [athlete.pk], post_template)
    )

    assert not blocked, "template_use locked the athlete's User row via its join"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_plan_create_draft_takes_the_coach_lock_before_the_link():
    """A draft must not hold the link while it waits for the coach row.

    `clear_demo` holds the coach row and then wants the demo links (#590), so a
    draft `plan_create` that took the link first and `_reserve_plan_draft`'s coach
    lock second ran link -> User against it: a deadlock. Post-fix the draft waits
    on the coach row holding nothing, so another transaction can still take the
    link while it waits.
    """
    coach = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    # Log in BEFORE the coach row is held: the login signal UPDATEs that row.
    client = Client()
    client.force_login(coach)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    request_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_rows, args=(User, [coach.pk], locked, release, holder_errors)
    )

    def post_draft():
        try:
            result["response"] = client.post(
                reverse("meso:plan_create", kwargs={"pk": relationship.athlete_id}),
                {"draft": "1"},
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            request_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=post_draft)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    blocked = _wait_until_a_backend_is_lock_blocked()
    link_is_free = False
    try:
        with transaction.atomic():
            list(
                CoachAthlete.objects.select_for_update(no_key=True, nowait=True).filter(
                    pk=relationship.pk
                )
            )
        link_is_free = True
    except DatabaseError:
        pass
    finally:
        release.set()
        holder.join(timeout=10)
        worker.join(timeout=10)

    assert blocked, "plan_create(draft) never waited for the coach row"
    assert link_is_free, "plan_create(draft) held the link while waiting for the coach"
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_plan_batch_deliver_waits_for_all_target_link_locks():
    coach = comp(UserFactory())
    source, _ = seed_source(coach=coach)
    targets = [
        CoachAthleteFactory(coach=coach, athlete=UserFactory()),
        CoachAthleteFactory(coach=coach, athlete=UserFactory()),
    ]

    def post_batch():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:plan_batch_deliver", kwargs={"plan_id": source.pk}),
            {"relationships": [target.pk for target in targets]},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(
            CoachAthlete, [target.pk for target in targets], post_batch
        )
    )

    assert blocked, "plan_batch_deliver did not lock its target links"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert all(target.plans.count() == 1 for target in targets)


def test_plan_batch_deliver_survives_a_live_clear_demo(monkeypatch):
    coach = comp(UserFactory())
    source, _ = seed_source(coach=coach)
    real_target = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    demo_athlete, demo_relationship, demo_plan = _demo_tree(coach)
    client = Client()
    client.force_login(coach)

    def post_batch():
        return client.post(
            reverse("meso:plan_batch_deliver", kwargs={"plan_id": source.pk}),
            {"relationships": [real_target.pk, demo_relationship.pk]},
        )

    response = _race_creator_against_clear_demo(
        monkeypatch,
        coach=coach,
        demo_athlete=demo_athlete,
        demo_relationship=demo_relationship,
        demo_plan=demo_plan,
        request_call=post_batch,
        pause_seam="cascade-locks",
    )

    assert response.status_code == 302
    assert response.url == reverse(
        "meso:deliver_plan",
        kwargs={"plan_id": source.pk},
    )
    copy = real_target.plans.get()
    assert copy.status == Plan.Status.ACTIVE
    assert copy.title == source.title


def test_roster_add_self_waits_for_the_coach_user_lock():
    coach = UserFactory()
    client = Client()
    client.force_login(coach)

    def post_add_self():
        return client.post(reverse("meso:roster_add_self"))

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [coach.pk], post_add_self)
    )

    assert blocked, "roster_add_self did not take the coach User lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert CoachAthlete.objects.filter(
        coach=coach, athlete=coach, is_self=True
    ).exists()


def test_roster_add_self_survives_a_live_clear_demo(monkeypatch):
    coach = UserFactory()
    demo_athlete, demo_relationship, demo_plan = _demo_tree(coach)
    client = Client()
    client.force_login(coach)

    def post_add_self():
        return client.post(reverse("meso:roster_add_self"))

    response = _race_creator_against_clear_demo(
        monkeypatch,
        coach=coach,
        demo_athlete=demo_athlete,
        demo_relationship=demo_relationship,
        demo_plan=demo_plan,
        request_call=post_add_self,
        pause_seam="coach-lock",
    )

    assert response.status_code == 302
    assert response.url == reverse("meso:roster")
    self_link = CoachAthlete.objects.get(coach=coach, athlete=coach)
    assert self_link.is_self
    assert self_link.is_active


# The hold-the-lock tests prove each creator takes its parent lock. The
# ``*_survives_a_live_clear_demo`` tests race the real ``demo.clear_demo`` via
# its ``_demo_athletes`` / ``lock_cascade_parents`` monkeypatch seams, with no
# production hook (#616). ``invite_claim`` has no PostgreSQL race test because
# deleting the coach also cascades the invite, so its isolated missing-parent
# state cannot be forced without a synthetic hook.


def test_athlete_request_coach_waits_for_the_coach_user_lock(monkeypatch):
    coach = CoachProfileFactory().user
    athlete = UserFactory()
    client = Client()
    client.force_login(athlete)
    monkeypatch.setattr(views, "send_coach_request_email", lambda **kwargs: True)

    def post_request():
        return client.post(
            reverse("meso:athlete_request_coach"), {"email": coach.email}
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [coach.pk], post_request)
    )

    assert blocked, "athlete_request_coach did not take the coach User lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()


def test_athlete_request_coach_survives_a_live_clear_demo(monkeypatch):
    coach = CoachProfileFactory().user
    demo_athlete, demo_relationship, demo_plan = _demo_tree(coach)
    athlete = UserFactory()
    client = Client()
    client.force_login(athlete)
    monkeypatch.setattr(views, "send_coach_request_email", lambda **kwargs: True)

    def post_request():
        return client.post(
            reverse("meso:athlete_request_coach"),
            {"email": coach.email},
        )

    response = _race_creator_against_clear_demo(
        monkeypatch,
        coach=coach,
        demo_athlete=demo_athlete,
        demo_relationship=demo_relationship,
        demo_plan=demo_plan,
        request_call=post_request,
        pause_seam="coach-lock",
    )

    assert response.status_code == 302
    assert response.url == reverse("meso:athlete_home")
    link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
    assert not link.is_demo
    assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST


def test_athlete_request_coach_waits_for_the_athlete_user_lock(monkeypatch):
    coach = CoachProfileFactory().user
    athlete = UserFactory()
    client = Client()
    client.force_login(athlete)
    monkeypatch.setattr(views, "send_coach_request_email", lambda **kwargs: True)

    def post_request():
        return client.post(
            reverse("meso:athlete_request_coach"), {"email": coach.email}
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [athlete.pk], post_request)
    )

    assert blocked, "athlete_request_coach did not take the athlete User lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).exists()


def test_athlete_request_coach_locks_both_users_before_the_link(monkeypatch):
    coach = CoachProfileFactory().user
    athlete = UserFactory()
    client = Client()
    client.force_login(athlete)
    monkeypatch.setattr(views, "send_coach_request_email", lambda **kwargs: True)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:athlete_request_coach"), {"email": coach.email}
        )

    locks = [
        (index, query["sql"])
        for index, query in enumerate(queries.captured_queries)
        if re.search(r"\bFOR (?:NO KEY )?UPDATE\b", query["sql"])
    ]
    user_locks = [item for item in locks if 'FROM "users_user"' in item[1]]
    link_locks = [item for item in locks if 'FROM "meso_coachathlete"' in item[1]]

    assert response.status_code == 302
    assert len(user_locks) == 1, f"expected one User lock, got: {user_locks}"
    assert len(link_locks) == 1, f"expected one CoachAthlete lock, got: {link_locks}"
    user_index, user_sql = user_locks[0]
    link_index, link_sql = link_locks[0]
    assert user_index < link_index, f"User lock must precede link lock: {locks}"
    assert '"users_user"."id" IN (' in user_sql
    assert coach.pk.hex in user_sql and athlete.pk.hex in user_sql
    assert re.search(r'ORDER BY (?:"users_user"\."id"|1) ASC', user_sql)
    assert "FOR NO KEY UPDATE" in user_sql and "FOR UPDATE" not in user_sql
    assert "FOR NO KEY UPDATE" in link_sql and "FOR UPDATE" not in link_sql


def test_coach_invite_waits_for_the_coach_user_lock(monkeypatch):
    coach = UserFactory()
    client = Client()
    client.force_login(coach)
    monkeypatch.setattr(views, "send_coach_invite_email", lambda **kwargs: True)

    def post_invite():
        return client.post(
            reverse("meso:coach_invite"), {"email": "locked-athlete@example.com"}
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [coach.pk], post_invite)
    )

    assert blocked, "coach_invite did not take the coach User lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert CoachInvite.objects.filter(
        coach=coach, email="locked-athlete@example.com"
    ).exists()


def test_coach_invite_survives_a_live_clear_demo(monkeypatch):
    coach = UserFactory()
    demo_athlete, demo_relationship, demo_plan = _demo_tree(coach)
    email = "clear-race-athlete@example.com"
    client = Client()
    client.force_login(coach)
    monkeypatch.setattr(views, "send_coach_invite_email", lambda **kwargs: True)

    def post_invite():
        return client.post(reverse("meso:coach_invite"), {"email": email})

    response = _race_creator_against_clear_demo(
        monkeypatch,
        coach=coach,
        demo_athlete=demo_athlete,
        demo_relationship=demo_relationship,
        demo_plan=demo_plan,
        request_call=post_invite,
        pause_seam="coach-lock",
    )

    assert response.status_code == 302
    assert response.url == reverse("meso:roster")
    invite = CoachInvite.objects.get(coach=coach, email=email)
    assert invite.status == CoachInvite.Status.PENDING
