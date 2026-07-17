"""Import Google-Sheets template workbook(s) as ONE template ``Plan``.

Phase 3 of the spreadsheet-parity plan (§5 "validate, don't bulk-load"):

    manage.py meso_import_template 402.xlsx --owner coach@example.com
    manage.py meso_import_template 101.xlsx 102.xlsx 103.xlsx
        --owner coach@example.com --title "Base 1-3"

Each ``.xlsx`` becomes one ``Mesocycle`` (in argument order — a template
*family* like 101→102→103 imports as one multi-block plan, §5), parsed by
``sheet_import.parse_workbook`` and materialized by the same
``seed_meso_demo.build_block`` the demo seeders use. The plan is a
**template** (``is_template=True``, no relationship, ``owner`` = the coach's
library, §3.4), resolved by ``(owner, title, is_template)`` so a re-run with
the same title updates in place (``build_block`` is idempotent on the P0
natural keys — never duplicates).
"""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.core.management.base import CommandParser
from django.db import transaction

from store_project.meso.management.commands.seed_meso_demo import build_block
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.sheet_import import SheetImportError
from store_project.meso.sheet_import import parse_workbook
from store_project.users.models import User


class Command(BaseCommand):
    help = (
        "Import template workbook(s) (.xlsx) as one template Plan — "
        "one Mesocycle per file, in argument order."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "paths",
            nargs="+",
            metavar="xlsx-path",
            help="Template workbook(s); each becomes one block, in order.",
        )
        parser.add_argument(
            "--owner",
            required=True,
            help="Email of the coach whose template library this joins.",
        )
        parser.add_argument(
            "--title",
            default="",
            help="Plan title (default: the first workbook's program tab name).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        owner = User.objects.filter(email=options["owner"]).first()
        if owner is None:
            raise CommandError(f"No user with email {options['owner']!r}.")

        parsed = []
        for path in options["paths"]:
            try:
                parsed.append((path, parse_workbook(path)))
            except SheetImportError as exc:
                raise CommandError(str(exc)) from exc
            except OSError as exc:
                raise CommandError(f"Cannot read {path}: {exc}") from exc

        title = options["title"] or parsed[0][1].tab
        plan, created = Plan.objects.update_or_create(
            owner=owner,
            title=title,
            is_template=True,
            defaults={"relationship": None, "status": Plan.Status.ACTIVE},
        )

        for order, (path, block) in enumerate(parsed):
            mesocycle, _ = Mesocycle.objects.update_or_create(
                plan=plan,
                order=order,
                defaults={"name": block.tab, "week_count": block.week_count},
            )
            build_block(mesocycle, block.block_spec)
            self.stdout.write(
                f"  - {path} → block {block.tab!r} (order {order}): "
                f"{block.day_count} days, {block.exercise_count} exercises, "
                f"{block.week_count} weeks, {block.cell_count} cells; "
                f"{len(block.skipped)} rows skipped"
            )
            for skip in block.skipped:
                self.stdout.write(f"      · r{skip.row} {skip.reason}: {skip.preview}")

        verb = "created" if created else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Template plan {plan.title!r} {verb} for {owner.email} "
                f"({len(parsed)} block{'s' if len(parsed) != 1 else ''})."
            )
        )
