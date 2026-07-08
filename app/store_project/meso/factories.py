try:
    import factory
    from django.utils import timezone
    from factory.django import DjangoModelFactory

    from store_project.users.factories import UserFactory

    from .models import AgentProposalBatch
    from .models import AthleteOneRm
    from .models import AthleteProfile
    from .models import CoachAthlete
    from .models import CoachInvite
    from .models import CoachProfile
    from .models import CoachSubscription
    from .models import Contraindication
    from .models import ExerciseSlot
    from .models import GroupMembership
    from .models import LoggedSet
    from .models import Mesocycle
    from .models import MesoGroup
    from .models import Plan
    from .models import Prescription
    from .models import PrescriptionOverride
    from .models import ProposedChange
    from .models import Session
    from .models import SessionLog
    from .models import SessionSlot
    from .models import Unit
    from .models import Week
    from .models import WeekDelivery

    class CoachProfileFactory(DjangoModelFactory):
        class Meta:
            model = CoachProfile

        user = factory.SubFactory(UserFactory)
        display_name = factory.LazyAttribute(lambda o: o.user.name)
        programming_style = factory.LazyFunction(
            lambda: ["Compound-first", "RPE-based load"]
        )
        avoid_rules = "machine-only days, untracked progressions"

    class AthleteProfileFactory(DjangoModelFactory):
        class Meta:
            model = AthleteProfile

        user = factory.SubFactory(UserFactory)

    class ContraindicationFactory(DjangoModelFactory):
        class Meta:
            model = Contraindication

        athlete = factory.SubFactory(UserFactory)
        text = "L knee — avoid deep knee flexion under load"

    class CoachAthleteFactory(DjangoModelFactory):
        class Meta:
            model = CoachAthlete

        coach = factory.SubFactory(UserFactory)
        athlete = factory.SubFactory(UserFactory)
        status = CoachAthlete.Status.ACTIVE
        invited_by = CoachAthlete.InvitedBy.COACH

    class CoachInviteFactory(DjangoModelFactory):
        class Meta:
            model = CoachInvite

        coach = factory.SubFactory(UserFactory)
        email = factory.Sequence(lambda n: f"invitee{n}@example.com")
        status = CoachInvite.Status.PENDING

    class CoachSubscriptionFactory(DjangoModelFactory):
        class Meta:
            model = CoachSubscription

        coach = factory.SubFactory(UserFactory)
        status = CoachSubscription.Status.FREE

    class PlanFactory(DjangoModelFactory):
        class Meta:
            model = Plan

        relationship = factory.SubFactory(CoachAthleteFactory)
        title = factory.Sequence(lambda n: f"Plan {n}")
        goal = "Hypertrophy"
        status = Plan.Status.DRAFT
        unit = Unit.KILOGRAMS

    class MesocycleFactory(DjangoModelFactory):
        class Meta:
            model = Mesocycle

        plan = factory.SubFactory(PlanFactory)
        name = factory.Sequence(lambda n: f"Block {n}")
        order = factory.Sequence(lambda n: n)
        week_count = 4

    class WeekFactory(DjangoModelFactory):
        class Meta:
            model = Week

        mesocycle = factory.SubFactory(MesocycleFactory)
        index = factory.Sequence(lambda n: n + 1)
        phase = "Accum"
        volume = 70
        intensity = 65
        is_deload = False
        is_current = False

    class SessionSlotFactory(DjangoModelFactory):
        """The fixed DAY definition (P0 fixed-lineup cutover)."""

        class Meta:
            model = SessionSlot

        mesocycle = factory.SubFactory(MesocycleFactory)
        day_number = factory.Sequence(lambda n: n + 1)
        name = factory.Sequence(lambda n: f"Day {n + 1}")
        bias = ""
        order = factory.Sequence(lambda n: n)

    class SessionFactory(DjangoModelFactory):
        class Meta:
            model = Session

        week = factory.SubFactory(WeekFactory)
        # A session is (week × slot) within ONE block, so the slot defaults onto
        # the week's mesocycle — a bare ``SessionFactory()`` must not straddle two
        # blocks (``session.cells()`` and week/slot joins assume a shared meso).
        session_slot = factory.SubFactory(
            SessionSlotFactory,
            mesocycle=factory.SelfAttribute("..week.mesocycle"),
        )

    class ExerciseSlotFactory(DjangoModelFactory):
        """The fixed EXERCISE row (P0 fixed-lineup cutover)."""

        class Meta:
            model = ExerciseSlot

        session_slot = factory.SubFactory(SessionSlotFactory)
        exercise = None
        name = factory.Sequence(lambda n: f"Exercise {n}")
        order = factory.Sequence(lambda n: n)
        tags = factory.LazyFunction(list)

    class PrescriptionFactory(DjangoModelFactory):
        """A cell = one ``ExerciseSlot`` (row) × one ``Week`` (P0 fixed-lineup cutover).

        ``week`` defaults to a fresh ``Week`` built on the **same mesocycle** as
        ``exercise_slot.session_slot.mesocycle`` — a real cell never crosses
        blocks, and ``unique(exercise_slot, week)`` plus every read site that
        joins a cell back to its slot's mesocycle would misbehave on a
        mismatched pair. Wired via ``factory.SelfAttribute("..exercise_slot...")``
        inside the ``week`` SubFactory, mirroring ``GroupMembershipFactory``'s
        same-coach wiring elsewhere in this module. Callers building a whole
        grid should still pass an explicit shared ``week=`` (or ``exercise_slot=``
        on an existing slot) so every cell in the fixture lands on the same week.
        """

        class Meta:
            model = Prescription

        exercise_slot = factory.SubFactory(ExerciseSlotFactory)
        week = factory.SubFactory(
            WeekFactory,
            mesocycle=factory.SelfAttribute("..exercise_slot.session_slot.mesocycle"),
        )
        sets = "3"
        reps = "10"
        load = "60"
        rpe = "7"
        note = ""

    class WeekDeliveryFactory(DjangoModelFactory):
        class Meta:
            model = WeekDelivery

        week = factory.SubFactory(WeekFactory)
        delivered_at = factory.LazyFunction(timezone.now)
        payload = factory.LazyFunction(dict)

    class AgentProposalBatchFactory(DjangoModelFactory):
        class Meta:
            model = AgentProposalBatch

        plan = factory.SubFactory(PlanFactory)
        coach = factory.LazyAttribute(lambda o: o.plan.relationship.coach)
        instruction = "Make Maya's week knee-safe and progress her deadlift."
        summary = ""
        model = "claude-opus-4-8"
        status = AgentProposalBatch.Status.PENDING

    class ProposedChangeFactory(DjangoModelFactory):
        class Meta:
            model = ProposedChange

        batch = factory.SubFactory(AgentProposalBatchFactory)
        kind = ProposedChange.Kind.SWAP
        day_label = "Day 1 · Lower"
        title = factory.Sequence(lambda n: f"Change {n}")
        before = "Before · 3×10"
        after = "After · 3×10"
        rationale = "Rationale."
        honors = ""
        order = factory.Sequence(lambda n: n)

    class SessionLogFactory(DjangoModelFactory):
        class Meta:
            model = SessionLog

        session = factory.SubFactory(SessionFactory)
        athlete = factory.SubFactory(UserFactory)
        status = SessionLog.Status.PENDING

    class LoggedSetFactory(DjangoModelFactory):
        class Meta:
            model = LoggedSet

        session_log = factory.SubFactory(SessionLogFactory)
        prescription = factory.SubFactory(PrescriptionFactory)
        set_number = factory.Sequence(lambda n: n + 1)
        reps = "10"
        load = "60"
        rpe = "7"

    class MesoGroupFactory(DjangoModelFactory):
        class Meta:
            model = MesoGroup

        coach = factory.SubFactory(UserFactory)
        name = factory.Sequence(lambda n: f"Group {n}")
        focus = "Hypertrophy"
        status = MesoGroup.Status.ACTIVE

    class GroupMembershipFactory(DjangoModelFactory):
        class Meta:
            model = GroupMembership

        group = factory.SubFactory(MesoGroupFactory)
        # The link's coach defaults to the group's coach so the membership is
        # same-coach-consistent without the caller wiring it up.
        relationship = factory.SubFactory(
            CoachAthleteFactory,
            coach=factory.SelfAttribute("..group.coach"),
            status=CoachAthlete.Status.ACTIVE,
        )

    class GroupPlanFactory(PlanFactory):
        """A plan rooted at a ``MesoGroup`` — a group's shared program (S1 Phase 2).

        Overrides ``PlanFactory``'s individual ``relationship`` to ``None`` and
        roots the plan at a group instead (the ``XOR`` root constraint).
        """

        relationship = None
        group = factory.SubFactory(MesoGroupFactory)

    class PrescriptionOverrideFactory(DjangoModelFactory):
        """A member's auto-adjust over a shared prescription (S1 Phase 3).

        Callers pass a same-group ``membership`` + ``prescription`` explicitly (the
        same-group invariant ``set_override``/``clean`` enforce); the SubFactory
        defaults only exist so the factory is constructible.
        """

        class Meta:
            model = PrescriptionOverride

        membership = factory.SubFactory(GroupMembershipFactory)
        prescription = factory.SubFactory(PrescriptionFactory)
        load_pct = 90

    class AthleteOneRmFactory(DjangoModelFactory):
        """An athlete's persisted, log-derived 1RM for a lift (S2 follow-up).

        ``key`` is derived in ``AthleteOneRm.save`` from ``exercise``/``name``, so
        the factory only sets the identity inputs + the value.
        """

        class Meta:
            model = AthleteOneRm

        athlete = factory.SubFactory(UserFactory)
        exercise = None
        name = factory.Sequence(lambda n: f"Exercise {n}")
        value = 100
        unit = Unit.KILOGRAMS

except ImportError:
    # Factory Boy is not available (likely in production).
    class CoachProfileFactory:
        pass

    class AthleteProfileFactory:
        pass

    class ContraindicationFactory:
        pass

    class CoachAthleteFactory:
        pass

    class CoachInviteFactory:
        pass

    class CoachSubscriptionFactory:
        pass

    class PlanFactory:
        pass

    class GroupPlanFactory:
        pass

    class MesocycleFactory:
        pass

    class WeekFactory:
        pass

    class SessionSlotFactory:
        pass

    class SessionFactory:
        pass

    class ExerciseSlotFactory:
        pass

    class PrescriptionFactory:
        pass

    class WeekDeliveryFactory:
        pass

    class SessionLogFactory:
        pass

    class LoggedSetFactory:
        pass

    class AgentProposalBatchFactory:
        pass

    class MesoGroupFactory:
        pass

    class GroupMembershipFactory:
        pass

    class PrescriptionOverrideFactory:
        pass

    class ProposedChangeFactory:
        pass

    class AthleteOneRmFactory:
        pass
