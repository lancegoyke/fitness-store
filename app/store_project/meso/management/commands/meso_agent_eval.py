"""Run the Meso agent golden eval corpus (Phase 4).

Exercises the real proposal pipeline against a corpus of realistic coach
instructions and checks model-agnostic invariants (responsive / grounded / safe),
so agent quality doesn't silently regress. Side-effect-free: the run is wrapped
in a transaction that is rolled back, so eval batches/changes are never persisted.

    manage.py meso_agent_eval                # real model; needs ANTHROPIC_API_KEY
    manage.py meso_agent_eval --dry-run      # scripted client, no network / key
    manage.py meso_agent_eval --plan-id 3    # evaluate against a specific plan

Exits non-zero if any case fails, so it can gate a scheduled quality check.
"""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from store_project.meso.agent import client as client_module
from store_project.meso.agent import evals
from store_project.meso.models import Plan


class Command(BaseCommand):
    help = "Run the Meso agent golden eval corpus."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plan-id",
            type=int,
            default=None,
            help="Plan to evaluate against (default: most recently modified).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Use a scripted client (no network / no API key needed).",
        )

    def handle(self, *args, **options):
        plan = self._resolve_plan(options["plan_id"])
        # §4b: the harness has no "coach's viewed block" to inherit, so it
        # evaluates against the plan's first block by order — same default the
        # designer grid/agent fall back to when nothing pins one explicitly.
        mesocycle = plan.mesocycles.order_by("order").first()
        if mesocycle is None:
            raise CommandError(f"Plan #{plan.pk} has no block to evaluate against.")

        if options["dry_run"]:
            client = evals.ScriptedEvalClient()
        else:
            client = client_module.get_default_client()
            if client is None:
                self.stdout.write(
                    self.style.WARNING(
                        "ANTHROPIC_API_KEY is not set — skipping. "
                        "Pass --dry-run to exercise the harness without a key."
                    )
                )
                return

        model = getattr(client, "model", "scripted")
        self.stdout.write(
            f"Evaluating {len(evals.GOLDEN_CASES)} case(s) against plan "
            f"#{plan.pk} ({plan.title}) with {model}\n"
        )

        # Roll the whole run back so eval proposals are never persisted.
        results = []
        try:
            with transaction.atomic():
                for case in evals.GOLDEN_CASES:
                    results.append(
                        evals.evaluate(plan, case, client=client, mesocycle=mesocycle)
                    )
                transaction.set_rollback(True)
        except Exception as exc:  # provider/db failure — surface, don't traceback
            raise CommandError(f"Eval run failed: {exc}") from exc

        failed = self._report(results)
        if failed:
            raise CommandError(f"{failed} case(s) failed.")
        self.stdout.write(self.style.SUCCESS("All cases passed."))

    def _resolve_plan(self, plan_id):
        if plan_id is not None:
            plan = Plan.objects.filter(pk=plan_id).first()
            if plan is None:
                raise CommandError(f"No plan with id {plan_id}.")
            return plan
        plan = (
            Plan.objects.exclude(status=Plan.Status.ARCHIVED)
            .order_by("-modified")
            .first()
        )
        if plan is None:
            raise CommandError(
                "No plan to evaluate. Run `manage.py seed_meso_demo` or pass --plan-id."
            )
        return plan

    def _report(self, results):
        failed = 0
        for r in results:
            tag = self.style.SUCCESS("PASS") if r.passed else self.style.ERROR("FAIL")
            self.stdout.write(
                f"  [{tag}] {r.case.name}: {r.n_changes} change(s), "
                f"{r.n_rejected} rejected"
            )
            for problem in r.failures:
                self.stdout.write(self.style.ERROR(f"        ✗ {problem}"))
            for note in r.warnings:
                self.stdout.write(self.style.WARNING(f"        ! {note}"))
            if not r.passed:
                failed += 1
        return failed
