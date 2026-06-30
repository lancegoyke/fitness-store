"""Email the owner when a paying coach's agent cost is eating their margin.

Agent-usage tracking Phase 3 (early warning). Phase 2's report computes each
paying coach's cost vs revenue and a binary ``flagged`` (cost already past
revenue). This command generalizes that to a tunable threshold (default 50% of
revenue, ``MESO_MARGIN_ALERT_THRESHOLD``) and *pushes* the result: it builds the
month's report, finds the at-risk paying coaches
(``agent_usage_report.margin_alerts``), and emails the owner a summary — so a
$1/seat tail-risk coach surfaces without anyone remembering to run a report.

Free/trial coaches never alert ($0 revenue is CAC by design, not a margin
problem). The estimated cost is the internal per-run number; the Anthropic invoice
stays authoritative. The monthly ``meso-agent-margin-alert`` schedule runs this
over the *previous* (closed) month via ``store_project.meso.tasks``.

    manage.py meso_agent_margin_alert                       # current month
    manage.py meso_agent_margin_alert --last-month          # the closed month (cron)
    manage.py meso_agent_margin_alert --month 2026-06
    manage.py meso_agent_margin_alert --threshold 0.75
    manage.py meso_agent_margin_alert --dry-run             # report, send nothing
"""

import logging
from decimal import Decimal
from decimal import InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from store_project.meso.billing import agent_usage_report as report_mod
from store_project.notifications.emails import send_margin_alert_email

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Email the owner about paying coaches whose agent cost outpaces revenue."

    def add_arguments(self, parser):
        window = parser.add_mutually_exclusive_group()
        window.add_argument(
            "--month",
            help="Calendar month as YYYY-MM (defaults to the current month).",
        )
        window.add_argument(
            "--last-month",
            action="store_true",
            help="Use the previous (closed) calendar month — the scheduled default.",
        )
        parser.add_argument(
            "--threshold",
            help=(
                "Alert fraction of revenue, e.g. 0.5 for 50% "
                "(defaults to MESO_MARGIN_ALERT_THRESHOLD)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the at-risk coaches without sending the email.",
        )

    def handle(self, *args, **options):
        start, end, label = self._window(options)
        threshold = self._threshold(options["threshold"])

        report = report_mod.build_report(start=start, end=end)
        alerts = report_mod.margin_alerts(report, threshold)

        pct = f"{threshold * 100:.0f}"
        self.stdout.write(
            f"Margin alert — {label}: {len(alerts)} paying coach(es) over {pct}% "
            f"of revenue on the agent."
        )
        for coach in alerts:
            ratio_pct = f"{coach.cost_to_revenue_ratio * 100:.0f}"
            self.stdout.write(
                f"  {coach.label} ({coach.billing_status}): "
                f"cost ${coach.totals.cost:.2f} of ${coach.revenue:.2f} revenue "
                f"({ratio_pct}%) · margin ${coach.margin:.2f}"
            )

        if not alerts:
            return
        if options["dry_run"]:
            self.stdout.write("Dry run — no email sent.")
            return

        try:
            sent = send_margin_alert_email(
                alerts=alerts, month_label=label, threshold=threshold
            )
        except Exception:  # best-effort: a mail failure must not fail the sweep
            logger.exception("Margin-alert email failed for %s", label)
            self.stderr.write(self.style.WARNING("Margin-alert email failed (logged)."))
            return
        if sent:
            self.stdout.write(self.style.SUCCESS("Alert email sent to the owner."))
        else:
            self.stdout.write("No admin address configured — email skipped.")

    def _window(self, options):
        """Resolve the ``(start, end, label)`` window from the mutually-exclusive opts."""
        if options["last_month"]:
            start, end = report_mod.previous_month_bounds()
        elif options["month"]:
            try:
                year, month = report_mod.parse_month(options["month"])
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            start, end = report_mod.month_bounds(year, month)
        else:
            start, end = report_mod.current_month_bounds()
        return start, end, start.strftime("%Y-%m")

    def _threshold(self, raw):
        """Resolve the alert fraction from the flag or the setting; validate it."""
        if raw is None:
            raw = settings.MESO_MARGIN_ALERT_THRESHOLD
        try:
            threshold = Decimal(str(raw))
        except InvalidOperation as exc:
            raise CommandError(f"--threshold must be a number, got {raw!r}.") from exc
        if threshold <= 0:
            raise CommandError("--threshold must be greater than zero.")
        return threshold
