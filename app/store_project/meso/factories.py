try:
    import factory
    from django.utils import timezone
    from factory.django import DjangoModelFactory

    from store_project.users.factories import UserFactory

    from .models import AthleteProfile
    from .models import CoachAthlete
    from .models import CoachProfile
    from .models import Contraindication
    from .models import ExercisePrescription
    from .models import LoggedSet
    from .models import Mesocycle
    from .models import Plan
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
