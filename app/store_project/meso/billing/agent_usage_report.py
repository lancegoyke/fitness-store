"""Roll captured agent-run usage up into a per-coach margin report (agent-usage v2).

Phase 1 (``agent_costs.py`` + the ``AgentProposalBatch`` usage columns) captured
per-run token usage and an estimated cost on the run ledger. This is the **read
side**: aggregate those rows for a calendar month into

* per-coach **cost vs revenue → margin** (and a flag when a *paying* coach's agent
  cost outruns what they pay — the $1/seat tail risk D13 called out),
* a per-(coach, client) breakdown to find the **heavy seats** (a client is the
  athlete on an individual plan, or the group on a group plan), and
* roll-ups by **model**, by **trigger**, and by **billing tier** (the COGS-vs-CAC
  split off each run's snapshotted ``billing_status``).

``eval`` runs are excluded everywhere — they're a golden-corpus quality check, not
coach usage (the count is surfaced as a footnote). The cost is the **internal
estimate** stored at write time (the Anthropic invoice stays authoritative);
revenue is the coach's *current* plan price (base + per active seat), an
approximation since per-month historical seat counts aren't stored. See
``docs/meso/agent-usage-plan.md``.
"""

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal

from django.db.models import Count
from django.utils import timezone

from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription

# Plan price (S6 Phase 6, D13) — the numeric source for the revenue math, mirroring
# the ``presenters.PRICE_SUMMARY`` marketing copy ("$9.99/mo + $1 per active
# athlete"). Strings so the Decimals are exact. Update alongside that copy.
BASE_PRICE_USD = Decimal("9.99")
SEAT_PRICE_USD = Decimal("1.00")

_ZERO = Decimal("0")

# Billing-tier buckets for the COGS-vs-CAC split (off each run's snapshot status).
PAID = "paid"  # a real Stripe subscription — agent cost is COGS against revenue
COMPED = "comped"  # owner / demo coaches — real cost, no revenue, never flagged
FREE_TRIAL = "free_trial"  # free / trial / churned — cost is CAC (acquisition)
TIERS = (PAID, COMPED, FREE_TRIAL)


def parse_month(value):
    """Parse a ``YYYY-MM`` string into a ``(year, month)`` tuple.

    Raises ``ValueError`` (with a friendly message) on anything malformed so the
    management command can surface a ``CommandError`` rather than a stack trace.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(
            f"Month must be YYYY-MM (e.g. 2026-06), got {value!r}."
        ) from exc
    return parsed.year, parsed.month


def month_bounds(year, month):
    """The ``[start, end)`` tz-aware datetimes spanning one calendar month.

    Half-open so a batch is counted in exactly one month (``created_at >= start``
    and ``< end``). Made aware in the current timezone, matching how the rest of
    the app reasons about month windows (``billing/access.py``).
    """
    start_naive = datetime(year, month, 1)
    if month == 12:
        end_naive = datetime(year + 1, 1, 1)
    else:
        end_naive = datetime(year, month + 1, 1)
    tz = timezone.get_current_timezone()
    return timezone.make_aware(start_naive, tz), timezone.make_aware(end_naive, tz)


def current_month_bounds():
    """``month_bounds`` for the month containing ``now`` — the report's default."""
    now = timezone.now()
    return month_bounds(now.year, now.month)


def cost_bucket(billing_status):
    """Map a run's snapshot ``billing_status`` to a COGS-vs-CAC tier.

    ``active``/``past_due`` → paid (cost is COGS); ``comped`` → comped (owner/demo,
    real cost but no revenue); everything else — ``free``/``trialing``,
    ``canceled``, or a blank legacy snapshot — → free/trial (the cost is CAC).
    """
    if billing_status in (
        CoachSubscription.Status.ACTIVE,
        CoachSubscription.Status.PAST_DUE,
    ):
        return PAID
    if billing_status == CoachSubscription.Status.COMPED:
        return COMPED
    return FREE_TRIAL


def monthly_revenue(status, seats):
    """The coach's current monthly plan price: base + per-seat, or $0 when unpaid.

    Only a live Stripe subscription (``active``/``past_due``) bills; free, trial,
    comped, canceled, or no row at all yield $0 (revenue we collect, not list
    price). ``seats`` is the coach's *current* billable active-athlete count — an
    approximation of the report month, since historical seat counts aren't stored.
    """
    if status in CoachSubscription.LIVE_STRIPE_STATUSES:
        return BASE_PRICE_USD + SEAT_PRICE_USD * seats
    return _ZERO


class Totals:
    """A mutable usage accumulator — runs, the four token buckets, and cost.

    ``cost`` sums only runs whose model was in the rate table; a run with an
    unknown model (cost ``None``) is counted in ``unknown_cost_runs`` instead of
    silently pricing as $0, so the report can flag it.
    """

    __slots__ = (
        "runs",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cost",
        "unknown_cost_runs",
    )

    def __init__(self):
        self.runs = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.cost = _ZERO
        self.unknown_cost_runs = 0

    def add(self, batch):
        self.runs += 1
        self.input_tokens += batch.input_tokens
        self.output_tokens += batch.output_tokens
        self.cache_creation_input_tokens += batch.cache_creation_input_tokens
        self.cache_read_input_tokens += batch.cache_read_input_tokens
        if batch.estimated_cost_usd is None:
            self.unknown_cost_runs += 1
        else:
            self.cost += batch.estimated_cost_usd

    @property
    def total_tokens(self):
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class ClientUsage:
    """One coach's spend on a single client — an athlete, or a group."""

    label: str
    is_group: bool
    totals: Totals


@dataclass
class CoachUsage:
    """One coach's month: cost vs revenue, the margin, and a per-client breakdown."""

    coach_id: object
    label: str
    billing_status: str  # the coach's *current* subscription status
    is_paid: bool
    billable_seats: int
    revenue: Decimal
    totals: Totals
    clients: list = field(default_factory=list)

    @property
    def margin(self):
        """Revenue minus estimated agent cost — negative means the run cost won."""
        return self.revenue - self.totals.cost

    @property
    def flagged(self):
        """A *paying* coach whose agent cost outran their revenue (the tail risk).

        Only paid coaches flag: a free/trial coach has $0 revenue by design, so any
        cost would trivially "exceed" it — that's CAC, not a margin problem.
        """
        return self.is_paid and self.totals.cost > self.revenue


@dataclass
class Report:
    start: datetime
    end: datetime
    coaches: list
    by_model: dict
    by_trigger: dict
    by_tier: dict
    totals: Totals
    eval_runs_excluded: int


def _attribution(plan):
    """``(key, label, is_group)`` for the client a batch's plan serves.

    A group plan attributes to the group (no single athlete); an individual plan to
    its athlete. ``key`` namespaces the two so an athlete and a group can't collide.
    """
    if plan.group_id is not None:
        return ("group", plan.group_id), f"Group: {plan.group.name}", True
    athlete = plan.relationship.athlete
    return ("athlete", athlete.id), athlete.display_name(), False


def build_report(*, start, end):
    """Aggregate the month's non-eval agent runs into a :class:`Report`.

    Coaches and their clients are sorted by estimated cost (then run count)
    descending, so the heaviest spenders surface first. Revenue and billable-seat
    counts are read from each coach's *current* subscription.
    """
    in_window = AgentProposalBatch.objects.filter(
        created_at__gte=start, created_at__lt=end
    )
    eval_runs_excluded = in_window.filter(
        trigger=AgentProposalBatch.Trigger.EVAL
    ).count()
    batches = in_window.exclude(trigger=AgentProposalBatch.Trigger.EVAL).select_related(
        "coach",
        "plan",
        "plan__relationship__athlete",
        "plan__group",
    )

    totals = Totals()
    by_model = {}
    by_trigger = {}
    by_tier = {tier: Totals() for tier in TIERS}
    # coach_id -> {"label", "totals", "clients": {key: ClientUsage}}
    coaches = {}

    for batch in batches:
        totals.add(batch)
        _bucket(by_model, batch.model or "(unset)").add(batch)
        _bucket(by_trigger, batch.get_trigger_display()).add(batch)
        by_tier[cost_bucket(batch.billing_status)].add(batch)

        acc = coaches.get(batch.coach_id)
        if acc is None:
            acc = {
                "label": batch.coach.display_name(),
                "totals": Totals(),
                "clients": {},
            }
            coaches[batch.coach_id] = acc
        acc["totals"].add(batch)

        key, label, is_group = _attribution(batch.plan)
        client = acc["clients"].get(key)
        if client is None:
            client = ClientUsage(label=label, is_group=is_group, totals=Totals())
            acc["clients"][key] = client
        client.totals.add(batch)

    coach_ids = list(coaches)
    subs = {
        sub.coach_id: sub
        for sub in CoachSubscription.objects.filter(coach_id__in=coach_ids)
    }
    seat_counts = {
        row["coach_id"]: row["n"]
        for row in CoachAthlete.objects.billable()
        .filter(coach_id__in=coach_ids)
        .values("coach_id")
        .annotate(n=Count("id"))
    }

    coach_usages = []
    for coach_id, acc in coaches.items():
        sub = subs.get(coach_id)
        status = sub.status if sub else CoachSubscription.Status.FREE
        seats = seat_counts.get(coach_id, 0)
        clients = sorted(
            acc["clients"].values(),
            key=lambda c: (c.totals.cost, c.totals.runs),
            reverse=True,
        )
        coach_usages.append(
            CoachUsage(
                coach_id=coach_id,
                label=acc["label"],
                billing_status=status,
                is_paid=status in CoachSubscription.LIVE_STRIPE_STATUSES,
                billable_seats=seats,
                revenue=monthly_revenue(status, seats),
                totals=acc["totals"],
                clients=clients,
            )
        )
    coach_usages.sort(key=lambda c: (c.totals.cost, c.totals.runs), reverse=True)

    return Report(
        start=start,
        end=end,
        coaches=coach_usages,
        by_model=by_model,
        by_trigger=by_trigger,
        by_tier=by_tier,
        totals=totals,
        eval_runs_excluded=eval_runs_excluded,
    )


def _bucket(mapping, key):
    """Return ``mapping[key]``, creating a fresh :class:`Totals` on first sight."""
    totals = mapping.get(key)
    if totals is None:
        totals = Totals()
        mapping[key] = totals
    return totals
