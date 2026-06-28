try:
    import factory
    from django.utils import timezone
    from factory.django import DjangoModelFactory

    from store_project.users.factories import UserFactory

    from .models import AgentProposalBatch
    from .models import AthleteProfile
    from .models import CoachAthlete
    from .models import CoachProfile
    from .models import Contraindication
    from .models import ExercisePrescription
    from .models import GroupMembership
    from .models import LoggedSet
    from .models import Mesocycle
    from .models import MesoGroup
    from .models import Plan
    from .models import PrescriptionOverride
    from .models import ProposedChange
    from .models import Session
    from .models import SessionLog
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

    class SessionFactory(DjangoModelFactory):
        class Meta:
            model = Session

        week = factory.SubFactory(WeekFactory)
        day_number = factory.Sequence(lambda n: n + 1)
        name = factory.Sequence(lambda n: f"Day {n + 1}")
        bias = ""
        order = factory.Sequence(lambda n: n)

    class ExercisePrescriptionFactory(DjangoModelFactory):
        class Meta:
            model = ExercisePrescription

        session = factory.SubFactory(SessionFactory)
        exercise = None
        name = factory.Sequence(lambda n: f"Exercise {n}")
        order = factory.Sequence(lambda n: n)
        sets = "3"
        reps = "10"
        load = "60"
        rpe = "7"
        note = ""
        tags = factory.LazyFunction(list)

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
        prescription = factory.SubFactory(ExercisePrescriptionFactory)
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
        prescription = factory.SubFactory(ExercisePrescriptionFactory)
        load_pct = 90

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

    class PlanFactory:
        pass

    class GroupPlanFactory:
        pass

    class MesocycleFactory:
        pass

    class WeekFactory:
        pass

    class SessionFactory:
        pass

    class ExercisePrescriptionFactory:
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
