"""PostgreSQL-only regression test for parse-at-commit's tolerance guard (5a).

``_upsert_parsed_set`` runs INSIDE ``athlete_cell_write``'s ``transaction
.atomic()`` block, after the athlete's cell text has already been saved. Its
contract (docs/meso/parse-at-commit-plan.md §11) is absolute: **a failure in
the parse/upsert must never cost the athlete their typed text.**

Catching the exception is not enough to honour that. When a *database* error
occurs inside an atomic block, Postgres puts the transaction into an aborted
state and Django marks it ``needs_rollback``; swallowing the exception does
not clear that flag, so the OUTER atomic block still rolls back on exit and
takes the already-saved ``cell.text`` with it. The nested
``transaction.atomic()`` savepoint in ``_upsert_parsed_set`` is what actually
contains the damage.

**Why this file exists separately.** SQLite does not abort a transaction on a
failed statement, so on the default in-memory test DB this scenario passes
whether or not the savepoint is present — a test there is worse than useless,
because it looks like coverage while discriminating nothing. Verified
empirically: with the savepoint removed, the SQLite version still passed and
the Postgres version below failed.

This is the same SQLite-vs-Postgres blind spot that let the "pending trigger
events" migration bug ship twice (see
``test_migration_0040_pending_triggers.py`` and the ``postgres`` job in
``.github/workflows/django.yml``) — different mechanism, identical lesson:
transaction semantics that prod depends on are simply not expressible on
SQLite.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \
        uv run pytest app/store_project/meso/tests/test_parse_at_commit_postgres.py
"""

import pytest
from django.db import connection

from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason=(
            "A failed statement only aborts the surrounding transaction on "
            "PostgreSQL; SQLite keeps the connection usable, so the outer "
            "block commits either way and the savepoint's effect is "
            "invisible."
        ),
    ),
]


def test_a_database_error_in_the_upsert_does_not_roll_back_the_cell_text(
    client, monkeypatch
):
    """A real aborted transaction must not cost the athlete their typed text."""

    def boom(_text):
        # A genuinely invalid query — NOT a hand-raised DatabaseError. Raising
        # ``DatabaseError`` from Python leaves the connection healthy and does
        # not reproduce the failure; the statement has to actually fail.
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM a_table_that_does_not_exist")

    monkeypatch.setattr("store_project.meso.views.parse_performed", boom)

    s = seed()
    client.force_login(s.athlete)
    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    # The response is unaffected: a parse/upsert failure is never the
    # athlete's problem.
    assert resp.status_code == 200

    # The load-bearing assertion. Without the savepoint this row is gone,
    # rolled back with the aborted transaction.
    cell = sub_cell(s.squat, 1)
    assert cell.text == "225 x 5", (
        "the athlete's cell text was rolled back by a failed upsert — the "
        "savepoint in _upsert_parsed_set is missing or ineffective"
    )

    # No derivative set survives the failure, and none is fabricated.
    assert not LoggedSet.objects.filter(source_line=cell).exists()
