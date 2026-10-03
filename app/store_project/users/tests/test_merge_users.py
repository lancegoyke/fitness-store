"""``merge_users`` refuses to delete an account that holds Meso history (#700).

The command never moves Meso data, so deleting the source would delete its own
logs (``SessionLog.athlete`` CASCADE) or, for a coach, hit RESTRICT on its
athletes' logs.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _confirm(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")


def _log(s):
    log = SessionLogFactory(session=s.session, athlete=s.athlete)
    row = LoggedSetFactory(session_log=log, prescription=s.squat)
    return log, row


def _merge(src, tgt, *extra):
    out = StringIO()
    call_command("merge_users", src.email, tgt.email, *extra, stdout=out)
    return out.getvalue()


def test_refuses_a_source_with_their_own_logs():
    s = seed()
    log, row = _log(s)
    target = UserFactory(points=7)

    with pytest.raises(CommandError, match="Meso training history"):
        _merge(s.athlete, target)

    assert User.objects.filter(pk=s.athlete.pk).exists()
    assert SessionLog.objects.filter(pk=log.pk).exists()
    assert LoggedSet.objects.filter(pk=row.pk).exists()
    target.refresh_from_db()
    assert target.points == 7


def test_refuses_a_coach_whose_athlete_has_logs_on_their_plan():
    s = seed()
    log, row = _log(s)
    target = UserFactory()

    with pytest.raises(CommandError, match="Meso training history"):
        _merge(s.coach, target)

    assert User.objects.filter(pk=s.coach.pk).exists()
    assert SessionLog.objects.filter(pk=log.pk).exists()
    assert LoggedSet.objects.filter(pk=row.pk).exists()


def test_merges_a_source_with_no_meso_logs():
    source = UserFactory(points=3)
    CoachAthleteFactory(coach=source, athlete=UserFactory())  # a link, no logs
    target = UserFactory(points=4)

    out = _merge(source, target)

    assert "Successfully merged" in out
    assert not User.objects.filter(pk=source.pk).exists()
    target.refresh_from_db()
    assert target.points == 7


def test_dry_run_reports_the_counts_and_changes_nothing():
    s = seed()
    log, row = _log(s)
    target = UserFactory()

    out = _merge(s.athlete, target, "--dry-run")

    assert "Meso session logs (own): 1" in out
    assert "a real run would refuse" in out
    assert User.objects.filter(pk=s.athlete.pk).exists()
    assert SessionLog.objects.filter(pk=log.pk).exists()
    assert LoggedSet.objects.filter(pk=row.pk).exists()
