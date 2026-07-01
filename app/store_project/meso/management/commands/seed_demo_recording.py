"""Seed a deterministic demo coach with known credentials for video recording.

Issue #388 (Level-1 demo): the automated, re-recordable walkthrough video drives
a real browser through the real allauth login form, so the recorder needs a
coach whose credentials are **known in advance** — not ``seed_meso_demo``'s
coach, whose password is a ``get_random_string(16)`` throwaway printed once to
stdout (fine for a human skimming it, useless for an unattended script). This
command is that command's deterministic sibling:

- the coach's email + password are **known** (``--email``/``--password`` →
  ``MESO_DEMO_COACH_EMAIL``/``MESO_DEMO_COACH_PASSWORD`` → a fixed default),
  and the password is **(re)set on every run** — a stale/rotated password from
  a prior recording session can never lock the recorder out;
- the coach is **comped** (``CoachSubscription.comp``) — the demo workspace (5
  athletes + a group) would otherwise trip the free-tier seat/agent gates and
  freeze the designer/agent mid-recording;
- the demo workspace is **reset** (``clear_demo`` → ``load_demo``) rather than
  topped up: ``load_demo`` alone upserts rows but keeps whatever a previous
  recording added on top (applied agent batches, their designer chat history,
  logged sets), and run N's video would show N-1 stale agent threads on
  camera. Every recording starts from the same pristine workspace (the same 5
  athletes + Maya's built/delivered/logged plan + the demo group);
- Maya Okonkwo — the demo's "hero" athlete — additionally gets a **usable**
  password (the same as the coach's) so an optional athlete-phone-view shot in
  the storyboard can log in as her too; her four demo siblings keep their
  unusable passwords (nobody needs to log in as them).

Guardrail: this creates (or overwrites) a **known-password, comped** account —
harmless on a throwaway dev/staging box, a real liability anywhere real. It
refuses to run unless ``settings.DEBUG`` is on, or ``--force`` is passed.
"""

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.core.management.base import CommandParser
from django.db import transaction

from store_project.meso.demo import clear_demo
from store_project.meso.demo import demo_email
from store_project.meso.demo import load_demo
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.users.models import User

DEFAULT_COACH_EMAIL = "demo-coach@demo.invalid"
DEFAULT_COACH_PASSWORD = "meso-demo-recording"
DEFAULT_COACH_NAME = "Demo Coach"

#: The demo's "hero" athlete (``seed_meso_demo.ATHLETES``'s ``"maya"`` slug) —
#: the one demo athlete given a usable, known password for the optional
#: athlete-phone-view recording step.
MAYA_SLUG = "maya"


class Command(BaseCommand):
    help = (
        "Seed a deterministic, known-password demo coach + full demo workspace "
        "for the demo-video recorder (dev/staging only; pass --force elsewhere)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--email",
            default=None,
            help=(
                "Coach email (default: $MESO_DEMO_COACH_EMAIL, else "
                f"{DEFAULT_COACH_EMAIL})."
            ),
        )
        parser.add_argument(
            "--password",
            default=None,
            help=(
                "Coach password, (re)set on every run (default: "
                "$MESO_DEMO_COACH_PASSWORD, else a fixed known default)."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print the result as a single JSON object instead of text lines.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow running with settings.DEBUG off (never do this on prod).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to seed a known-password, comped demo coach with "
                "DEBUG off — this would create/overwrite that account "
                "wherever it runs. Pass --force if you really mean it (e.g. a "
                "disposable staging box)."
            )

        email = (
            options["email"]
            or os.environ.get("MESO_DEMO_COACH_EMAIL")
            or DEFAULT_COACH_EMAIL
        )
        password = (
            options["password"]
            or os.environ.get("MESO_DEMO_COACH_PASSWORD")
            or DEFAULT_COACH_PASSWORD
        )

        coach = self._ensure_coach(email, password)
        CoachProfile.objects.get_or_create(user=coach)
        CoachSubscription.comp(coach)
        # Reset, don't top up: drop whatever a previous recording layered onto
        # the workspace (applied batches + their designer chat thread, logged
        # sets) so every video starts from the identical pristine state.
        clear_demo(coach)
        load_demo(coach)
        athlete_email = self._ensure_maya_login(coach, password)

        result = {"coach_email": coach.email, "athlete_email": athlete_email}
        if options["json"]:
            self.stdout.write(json.dumps(result))
            return

        self.stdout.write(self.style.SUCCESS(f"✓ Demo coach ready: {coach.email}"))
        if athlete_email:
            self.stdout.write(f"  - Maya (athlete) login: {athlete_email}")
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  - Maya's demo athlete row was not found; her login was "
                    "not set up."
                )
            )

    # -- coach ----------------------------------------------------------------

    def _ensure_coach(self, email, password):
        """The demo coach, with a **known, always-current** password + name."""
        coach, _created = User.objects.get_or_create(
            email=email, defaults={"username": email, "name": DEFAULT_COACH_NAME}
        )
        coach.name = DEFAULT_COACH_NAME
        coach.set_password(password)
        coach.save()
        return coach

    # -- Maya's athlete login ---------------------------------------------------

    def _ensure_maya_login(self, coach, password):
        """Give Maya's demo athlete a usable, known password.

        Non-fatal if her row is somehow absent (e.g. ``load_demo`` changes
        shape underneath this command) — the recorder can still shoot the
        coach-side flows; only the optional athlete-phone-view step needs her.
        """
        maya_email = demo_email(coach, MAYA_SLUG)
        maya = User.objects.filter(email=maya_email).first()
        if maya is None:
            self.stderr.write(
                self.style.WARNING(
                    f"Maya's demo athlete row ({maya_email}) was not found "
                    "(did load_demo run?) — skipping her login setup."
                )
            )
            return None
        maya.set_password(password)
        maya.save(update_fields=["password"])
        return maya.email
