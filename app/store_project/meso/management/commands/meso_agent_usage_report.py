"""Report Meso agent usage + estimated cost vs revenue for a calendar month.

Phase 1 captured per-run token usage and an estimated cost on the
``AgentProposalBatch`` ledger; this command is the owner-facing read-out
(agent-usage v2). For a month it prints per-coach **cost vs revenue → margin**
(flagging any *paying* coach whose agent cost outran their plan — the $1/seat tail
risk D13 called out), a per-(coach, client) breakdown to find the heavy seats, and
roll-ups by model / trigger / billing tier (the COGS-vs-CAC split). ``eval`` runs
are excluded (a quality check, not coach usage).

The cost is the **internal estimate** stored at write time — the Anthropic invoice
is authoritative; this exists to attribute that org-level total per coach/seat and
watch the margin. See ``docs/meso/agent-usage-plan.md``.

    manage.py meso_agent_usage_report                # the current month
    manage.py meso_agent_usage_report --month 2026-06
    manage.py meso_agent_usage_report --month 2026-06 --json
"""

import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from store_project.meso.billing import agent_usage_report as report_mod


def _usd(value, places=4):
    """Format a Decimal as USD; an em-dash for a None (unknown-model) cost."""
    if value is None:
        return "—"
    return f"${value:.{places}f}"


def _serialize_totals(totals):
    return {
        "runs": totals.runs,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
        "cache_read_input_tokens": totals.cache_read_input_tokens,
        "total_tokens": totals.total_tokens,
        "cost": str(totals.cost),
        "unknown_cost_runs": totals.unknown_cost_runs,
    }


def _serialize(report):
    """A JSON-safe dict of the report (Decimals → strings) for ``--json``."""
    return {
        "start": report.start.isoformat(),
        "end": report.end.isoformat(),
        "eval_runs_excluded": report.eval_runs_excluded,
        "totals": _serialize_totals(report.totals),
        "by_model": {k: _serialize_totals(v) for k, v in report.by_model.items()},
        "by_trigger": {k: _serialize_totals(v) for k, v in report.by_trigger.items()},
        "by_tier": {k: _serialize_totals(v) for k, v in report.by_tier.items()},
        "coaches": [
            {
                "coach_id": str(c.coach_id),
                "label": c.label,
                "billing_status": c.billing_status,
                "is_paid": c.is_paid,
                "flagged": c.flagged,
                "billable_seats": c.billable_seats,
                "revenue": str(c.revenue),
                "margin": str(c.margin),
                "totals": _serialize_totals(c.totals),
                "clients": [
                    {
                        "label": cl.label,
                        "is_group": cl.is_group,
                        "totals": _serialize_totals(cl.totals),
                    }
                    for cl in c.clients
                ],
            }
            for c in report.coaches
        ],
    }


class Command(BaseCommand):
    help = (
        "Per-coach Meso agent usage, estimated cost, and margin for a calendar month."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            help="Calendar month as YYYY-MM (defaults to the current month).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the report as JSON instead of a text table.",
        )

    def handle(self, *args, **options):
        if options["month"]:
            try:
                year, month = report_mod.parse_month(options["month"])
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            start, end = report_mod.month_bounds(year, month)
        else:
            start, end = report_mod.current_month_bounds()

        report = report_mod.build_report(start=start, end=end)

        if options["json"]:
            self.stdout.write(json.dumps(_serialize(report), indent=2))
            return

        self._render(report)

    def _render(self, report):
        w = self.stdout.write
        label = report.start.strftime("%Y-%m")
        w(f"Meso agent usage — {label} ({report.start.date()} → {report.end.date()})")
        w("Estimated cost is internal; the Anthropic invoice is authoritative.")
        w("")

        t = report.totals
        suffix = ""
        if t.unknown_cost_runs:
            suffix = f" · {t.unknown_cost_runs} run(s) with an unpriced model"
        excluded = ""
        if report.eval_runs_excluded:
            excluded = f"   ({report.eval_runs_excluded} eval run(s) excluded)"
        w(
            f"Totals: {t.runs} run(s) · {t.total_tokens:,} tokens · "
            f"est. {_usd(t.cost)}{suffix}{excluded}"
        )

        if not report.coaches:
            w("")
            w("No agent runs in this month.")
            return

        self._render_rollup(w, "By billing tier", report.by_tier)
        self._render_rollup(w, "By model", report.by_model)
        self._render_rollup(w, "By trigger", report.by_trigger)

        w("")
        w("Per coach (highest cost first):")
        for coach in report.coaches:
            flag = "[FLAG] " if coach.flagged else ""
            w(
                f"  {flag}{coach.label} "
                f"({coach.billing_status}, {coach.billable_seats} seat(s))"
            )
            w(
                f"      {coach.totals.runs} run(s) · "
                f"{coach.totals.total_tokens:,} tokens · "
                f"cost {_usd(coach.totals.cost)} · "
                f"revenue {_usd(coach.revenue, 2)} · "
                f"margin {_usd(coach.margin)}"
            )
            for client in coach.clients:
                w(
                    f"        {client.label}: {client.totals.runs} run(s), "
                    f"{_usd(client.totals.cost)}"
                )

    def _render_rollup(self, w, title, mapping):
        if not mapping:
            return
        w("")
        w(f"{title} (cost):")
        rows = sorted(
            mapping.items(), key=lambda kv: (kv[1].cost, kv[1].runs), reverse=True
        )
        for key, totals in rows:
            note = ""
            if totals.unknown_cost_runs:
                note = f" ({totals.unknown_cost_runs} unpriced)"
            w(
                f"  {key}: {totals.runs} run(s), "
                f"{totals.total_tokens:,} tokens, {_usd(totals.cost)}{note}"
            )
