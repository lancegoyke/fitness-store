"""Agent slice — a duplicate ``run_proposal_job`` must not reopen a batch (#558).

``_persist_result`` (``store_project.meso.agent.service``) used to flip a
batch's status to ``pending`` unconditionally. The background job's batch is
created ``drafting`` by one request and resolved by a worker later — so
between those two moments the coach can already have applied or dismissed it.
If ``run_proposal_job`` ever ran a SECOND time for the same batch id, the old
code would happily reopen an ``applied``/``dismissed``/``failed`` batch,
overwrite its summary and usage/cost columns with the duplicate run's numbers,
and insert a fresh set of ``ProposedChange`` rows next to (or instead of) the
ones the coach already acted on — rows a coach could then apply a second time.

**Why this isn't known to happen today.** django-q (``Q_CLUSTER`` in
``config/settings/base.py``) sets ``retry`` (600s) longer than ``timeout``
(300s) and ``max_attempts`` to 1, specifically so a still-running task is never
picked up again before it finishes, and a finished one is never retried. There
is no known caller — cron, admin action, a coach double-clicking "Draft with
AI" — that invokes ``run_proposal_job`` twice for one batch id. This file
exists so that invariant does not live ONLY in that queue configuration: the
fix adds an ``expect_status`` re-check (lock the batch row, confirm it is
still ``drafting``, discard the whole run otherwise) directly in
``_persist_result``, and these tests pin that the re-check — not the queue
settings — is what makes a duplicate run harmless.

No threads, no ``select_for_update`` contention, no Postgres-only behavior:
every scenario here is a plain sequential call, so this runs on the default
SQLite test database like any other meso test.
"""

import pytest

from store_project.meso.agent import apply as agent_apply
from store_project.meso.agent import client as client_module
from store_project.meso.agent import service
from store_project.meso.models import AgentProposalBatch
from store_project.meso.tests.test_agent_service import FakeClient
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db

SERVICE_LOGGER = "store_project.meso.agent.service"


def _first_run_result(presc):
    """The batch's real, first-run proposal.

    What any assertion below treats as "the coach's data" that must survive
    every duplicate that follows.
    """
    return {
        "summary": "First run: knee-safe swap.",
        "changes": [
            {
                "kind": "swap",
                "prescription_id": presc.pk,
                "title": "Back Squat → Box Squat",
                "before": "Back Squat",
                "after": "Box Squat",
                "rationale": "Shorter range.",
                "introduces_exercise": "Box Squat",
            }
        ],
    }


def _duplicate_run_result(presc):
    """A second run's result.

    Deliberately a DIFFERENT kind, title, and summary than
    ``_first_run_result`` so any leak (a stray write, a summary overwrite, an
    extra row) is unmistakable rather than coincidentally matching.
    """
    return {
        "summary": "DUPLICATE RUN: unrelated progress — must never be seen.",
        "changes": [
            {
                "kind": "progress",
                "prescription_id": presc.pk,
                "title": "DUPLICATE Back Squat → 999 kg",
                "rationale": "A duplicate run's own reasoning.",
                "new_load": "999 kg",
            }
        ],
    }


class _ClientWithUsage:
    """Like ``FakeClient`` but reports non-zero, rated usage.

    A bare-dict fake always records zero token usage (``client.normalize_result``
    coerces a dict to empty ``RunUsage``), so it can't distinguish "the second
    run's usage never overwrote the first's" from "usage is always zero
    anyway." This client returns a real ``ProposalResult`` with a caller-chosen
    ``RunUsage``, and reports a model that IS in ``agent_costs.RATES``
    (``claude-opus-4-8``, unlike the other fakes' ``"...-test"`` model), so
    ``estimated_cost_usd`` is a real, comparable, non-``None`` number too.
    """

    model = "claude-opus-4-8"

    def __init__(self, data, usage):
        self._result = client_module.ProposalResult(data=data, usage=usage)

    def propose(self, *, context, instruction):
        return self._result


FIRST_RUN_USAGE = client_module.RunUsage(
    input_tokens=1_000, output_tokens=200, api_calls=1
)
DUPLICATE_RUN_USAGE = client_module.RunUsage(
    input_tokens=999_999, output_tokens=999_999, api_calls=1
)


class TestDuplicateRunAfterApplyIsDiscarded:
    """The headline regression: an APPLIED batch must not be reopened."""

    def test_duplicate_run_after_apply_changes_nothing(self, caplog):
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(
            plan, "go", coach=plan.coach, mesocycle=plan.mesocycles.first()
        )

        # First run: ordinary drafting → pending, one change persisted, real
        # (rated, non-zero) usage recorded.
        first_batch, first_rejected = service.run_proposal_job(
            batch.pk,
            client=_ClientWithUsage(_first_run_result(presc), FIRST_RUN_USAGE),
        )
        assert first_rejected == []
        assert first_batch.status == AgentProposalBatch.Status.PENDING

        # The coach reviews and applies before any duplicate run arrives.
        agent_apply.apply_batch(first_batch)
        first_batch.refresh_from_db()
        assert first_batch.status == AgentProposalBatch.Status.APPLIED

        first_summary = first_batch.summary
        first_titles = list(
            first_batch.changes.order_by("pk").values_list("title", flat=True)
        )
        first_change_count = first_batch.changes.count()
        first_input_tokens = first_batch.input_tokens
        first_cost = first_batch.estimated_cost_usd
        assert first_change_count == 1
        assert first_cost is not None  # a rated model — sanity on the setup

        # A duplicate run for the SAME batch id: the scenario #558 describes.
        # Nothing in the app is known to trigger this (see the module
        # docstring) — this pins what must happen if it ever did.
        with caplog.at_level("WARNING", logger=SERVICE_LOGGER):
            dup_batch, dup_rejected = service.run_proposal_job(
                batch.pk,
                client=_ClientWithUsage(
                    _duplicate_run_result(presc), DUPLICATE_RUN_USAGE
                ),
            )

        dup_batch.refresh_from_db()
        # Still applied — the duplicate run did not reopen it.
        assert dup_batch.status == AgentProposalBatch.Status.APPLIED
        # The summary is still the first run's, not the duplicate's.
        assert dup_batch.summary == first_summary
        assert "DUPLICATE" not in dup_batch.summary
        # No ProposedChange rows from the second run exist: the count is
        # unchanged and the surviving rows are still the first run's.
        assert dup_batch.changes.count() == first_change_count
        assert (
            list(dup_batch.changes.order_by("pk").values_list("title", flat=True))
            == first_titles
        )
        # The second run's usage/cost never overwrote the first run's numbers
        # (1,000 vs 999,999 tokens — nothing coincidental about a match here).
        assert dup_batch.input_tokens == first_input_tokens
        assert dup_batch.estimated_cost_usd == first_cost
        # The duplicate's own return value: nothing was rejected (nothing was
        # even validated — the run was discarded before that point).
        assert dup_rejected == []

        # The discard was logged, so a duplicate run leaves a trail even
        # though it writes nothing.
        messages = [r.message for r in caplog.records]
        assert any(
            f"batch {batch.pk}" in m and "discarding a duplicate run's result" in m
            for m in messages
        )


def _resolve_via_dismiss(batch, presc):
    """Resolve ``batch`` to a first, ordinary PENDING run, then DISMISS it.

    The "reviewed but rejected outright" resolved status, as opposed to
    APPLIED (covered by ``TestDuplicateRunAfterApplyIsDiscarded`` above).
    """
    first_batch, _ = service.run_proposal_job(
        batch.pk, client=FakeClient(_first_run_result(presc))
    )
    agent_apply.dismiss_batch(first_batch)
    first_batch.refresh_from_db()
    return first_batch


def _resolve_via_provider_failure(batch, presc):
    """Resolve ``batch`` to FAILED via a first run whose NETWORK call blows up.

    This is a different code path to a resolved batch than the other two
    (``_fail`` sets ``status=FAILED`` directly — it never reaches
    ``_persist_result``/``expect_status`` at all, because the client raised
    before there was any result to persist). It still leaves the batch
    resolved, and the guard must refuse to reopen it exactly as it refuses an
    APPLIED or DISMISSED one — a duplicate run arriving after a real provider
    failure must not clobber the recorded error with a late success.
    """

    class BoomClient:
        model = "claude-opus-4-8-test"

        def propose(self, *, context, instruction):
            raise RuntimeError("provider is down")

    first_batch, _ = service.run_proposal_job(batch.pk, client=BoomClient())
    return first_batch


@pytest.mark.parametrize(
    "resolve, expected_status",
    [
        (_resolve_via_dismiss, AgentProposalBatch.Status.DISMISSED),
        (_resolve_via_provider_failure, AgentProposalBatch.Status.FAILED),
    ],
    ids=["dismissed", "failed"],
)
class TestDuplicateRunDoesNotReopenOtherResolvedStatuses:
    """The same ``expect_status`` guard, exercised against every OTHER status.

    Not just APPLIED: every other terminal status a batch can land in. If the
    guard were accidentally narrowed to "not applied" instead of "not still
    drafting", these would catch it.
    """

    def test_duplicate_run_is_discarded(self, resolve, expected_status, caplog):
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(
            plan, "go", coach=plan.coach, mesocycle=plan.mesocycles.first()
        )
        first_batch = resolve(batch, presc)
        assert first_batch.status == expected_status

        first_summary = first_batch.summary
        first_error = first_batch.error
        first_change_count = first_batch.changes.count()

        with caplog.at_level("WARNING", logger=SERVICE_LOGGER):
            dup_batch, dup_rejected = service.run_proposal_job(
                batch.pk, client=FakeClient(_duplicate_run_result(presc))
            )

        dup_batch.refresh_from_db()
        assert dup_batch.status == expected_status
        assert dup_batch.summary == first_summary
        assert dup_batch.error == first_error
        assert dup_batch.changes.count() == first_change_count
        assert dup_rejected == []

        messages = [r.message for r in caplog.records]
        assert any(
            f"batch {batch.pk}" in m and "discarding a duplicate run's result" in m
            for m in messages
        )


class TestFirstRunOnADraftingBatchStillWorks:
    """A control: the guard must fire on a MISMATCHED status, not on every run.

    Every test above discards a run and could, in principle, be satisfied by a
    guard that discards UNCONDITIONALLY (e.g. an ``expect_status`` check that
    was accidentally inverted). Pin the ordinary path here too, in this same
    file, so a guard that is too eager fails here rather than only showing up
    as a mysterious "the agent never proposes anything" bug elsewhere.
    """

    def test_first_run_flips_drafting_to_pending_and_persists(self):
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(
            plan, "go", coach=plan.coach, mesocycle=plan.mesocycles.first()
        )

        result_batch, rejected = service.run_proposal_job(
            batch.pk, client=FakeClient(_first_run_result(presc))
        )

        result_batch.refresh_from_db()
        assert result_batch.status == AgentProposalBatch.Status.PENDING
        assert result_batch.summary == "First run: knee-safe swap."
        assert rejected == []
        assert result_batch.changes.count() == 1


class TestSynchronousPathIsUnaffected:
    """``propose_changes`` passes NO ``expect_status`` — pin that it stays that way.

    ``propose_changes`` creates its own batch ``pending``, inside its own
    transaction, and calls ``_persist_result`` immediately after — there is no
    window between "the batch exists" and "this call resolves it" for anyone
    else to act in, so the ``expect_status`` re-check would have nothing to
    protect against here and everything to break: a hard-coded ``drafting``
    check would reject every synchronous run outright, since a batch made by
    ``propose_changes`` is never ``drafting``. This is the trap the
    ``expect_status=None`` default exists for — a future edit that "tidies"
    ``_persist_result`` into an unconditional status check must fail here
    first.
    """

    def test_propose_changes_persists_onto_its_own_pending_batch(self):
        plan, _, presc = make_plan()
        fake = FakeClient(_first_run_result(presc))

        batch, rejected = service.propose_changes(
            plan,
            "go",
            coach=plan.coach,
            mesocycle=plan.mesocycles.first(),
            client=fake,
        )

        assert rejected == []
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.summary == "First run: knee-safe swap."
        assert batch.changes.count() == 1


class TestReturnValueSyncsToDatabaseOnDiscard:
    """On a discard, ``run_proposal_job``'s return value must be honest.

    ``run_proposal_job`` fetches ``batch`` ONCE, at the top of the function,
    before the (possibly slow) network call. ``_persist_result`` then re-reads
    the row's status a moment later, under its own lock, to decide whether to
    discard. Between those two reads the row can have moved — that gap is
    exactly the race #558 is about. A client whose ``propose`` mutates the row
    out from under the job reproduces that gap deterministically, without any
    thread: the row is still ``drafting`` when ``run_proposal_job`` reads it,
    but ``applied`` by the time ``_persist_result`` checks again a moment
    later — standing in for "the coach applied it while the network call was
    in flight," the real window this guard closes.

    Without the fix's ``batch.status = locked.status`` line, the caller would
    get back the very instance ``run_proposal_job`` fetched at the top —
    stamped with the ``drafting`` it read BEFORE the network call — even
    though the database had already moved on to ``applied``. This pins that
    the returned instance reports the database's answer, not its own stale
    first read.
    """

    def test_discarded_runs_batch_reflects_the_database_not_the_stale_read(self):
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(
            plan, "go", coach=plan.coach, mesocycle=plan.mesocycles.first()
        )

        class ApplyMidFlightClient:
            """Applies ``batch`` out from under the running job.

            Fires the moment the "network" call is made, simulating a coach
            action that lands while ``run_proposal_job``'s own in-memory
            ``batch`` still thinks it's ``drafting``.
            """

            model = "claude-opus-4-8-test"

            def propose(self, *, context, instruction):
                AgentProposalBatch.objects.filter(pk=batch.pk).update(
                    status=AgentProposalBatch.Status.APPLIED
                )
                return _duplicate_run_result(presc)

        result_batch, rejected = service.run_proposal_job(
            batch.pk, client=ApplyMidFlightClient()
        )

        assert rejected == []
        assert result_batch.pk == batch.pk
        # The in-memory instance handed back must agree with the database —
        # never the ``drafting`` it read before the "network" call ran.
        assert result_batch.status == AgentProposalBatch.Status.APPLIED
        result_batch.refresh_from_db()
        assert result_batch.status == AgentProposalBatch.Status.APPLIED
        assert result_batch.changes.count() == 0
