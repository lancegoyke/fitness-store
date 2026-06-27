try:
    import factory
    from factory.django import DjangoModelFactory

    from store_project.users.factories import UserFactory

    from .models import AthleteProfile
    from .models import CoachAthlete
    from .models import CoachProfile
    from .models import Contraindication

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
