import pytest

from store_project.meso.models import CoachAthlete
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_pair():
    return UserFactory(), UserFactory()


class TestInviteStateMachine:
    def test_coach_invite_creates_pending(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        assert link.status == CoachAthlete.Status.PENDING_COACH_INVITE
        assert link.invited_by == CoachAthlete.InvitedBy.COACH
        assert link.recipient() == athlete
        assert link.is_pending

    def test_athlete_request_creates_pending(self):
        coach, athlete = make_pair()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert link.invited_by == CoachAthlete.InvitedBy.ATHLETE
        assert link.recipient() == coach

    def test_accept_activates(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        link.accept()
        assert link.is_active
        assert link.responded_at is not None

    def test_decline(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        link.decline()
        assert link.status == CoachAthlete.Status.DECLINED
        assert link.responded_at is not None

    def test_end_only_from_active(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        with pytest.raises(InvalidTransition):
            link.end()
        link.accept()
        link.end()
        assert link.status == CoachAthlete.Status.ENDED
        assert link.ended_at is not None

    def test_cannot_accept_non_pending(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        link.accept()
        with pytest.raises(InvalidTransition):
            link.accept()

    def test_cannot_coach_self(self):
        coach = UserFactory()
        with pytest.raises(InvalidTransition):
            CoachAthlete.invite(coach=coach, athlete=coach)

    def test_reinvite_reopens_after_decline(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        old_token = link.token
        link.decline()
        reopened = CoachAthlete.invite(coach=coach, athlete=athlete)
        assert reopened.pk == link.pk  # same row — unique(coach, athlete)
        assert reopened.status == CoachAthlete.Status.PENDING_COACH_INVITE
        assert reopened.token != old_token  # fresh token
        assert reopened.responded_at is None

    def test_reinvite_reopens_after_end(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        link.accept()
        link.end()
        reopened = CoachAthlete.invite(coach=coach, athlete=athlete)
        assert reopened.pk == link.pk
        assert reopened.status == CoachAthlete.Status.PENDING_COACH_INVITE
        assert reopened.ended_at is None

    def test_invite_idempotent_while_pending(self):
        coach, athlete = make_pair()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        again = CoachAthlete.invite(coach=coach, athlete=athlete)
        assert again.pk == link.pk
        assert again.token == link.token


class TestScopingManagers:
    def test_active_for_coach_excludes_others(self):
        coach = UserFactory()
        other_coach = UserFactory()
        mine = CoachAthlete.invite(coach=coach, athlete=UserFactory())
        mine.accept()
        # pending for the same coach — not active
        CoachAthlete.invite(coach=coach, athlete=UserFactory())
        # another coach's active link
        theirs = CoachAthlete.invite(coach=other_coach, athlete=UserFactory())
        theirs.accept()

        roster = list(CoachAthlete.objects.for_coach(coach).active())
        assert roster == [mine]

    def test_for_athlete_spans_multiple_coaches(self):
        athlete = UserFactory()
        c1 = CoachAthlete.invite(coach=UserFactory(), athlete=athlete)
        c1.accept()
        c2 = CoachAthlete.request(athlete=athlete, coach=UserFactory())
        c2.accept()
        coaches = CoachAthlete.objects.for_athlete(athlete).active()
        assert coaches.count() == 2
