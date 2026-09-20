"""#567/#568 P1-D: the warn read must run outside the write's transaction.

The warn read must not be able to touch a write that already committed. A
prior version of the #568 fix moved ``_cell_warn_or_false`` from the
response construction into ``athlete_cell_write``'s ``transaction.atomic()``
block, as its LAST statement, wrapped in its OWN nested ``transaction
.atomic()`` savepoint (mirroring ``_upsert_parsed_set``'s "load-bearing, not
decorative" nested atomic by analogy). That analogy does not hold: a single
failed statement recovers fine through an ordinary savepoint (Postgres
accepts ``ROLLBACK TO SAVEPOINT`` even from an aborted transaction, which is
exactly what makes ``_upsert_parsed_set``'s OWN nested atomic work). The
review's chain needs something stronger than one failed statement — a
genuinely dead connection (a pgbouncer ``server_lifetime`` cycle, a mid-
request DB restart), where the savepoint's own ROLLBACK-TO-SAVEPOINT recovery
ALSO fails. THAT is what makes Django mark ``connection.needs_rollback =
True`` in a way no enclosing savepoint can absorb, and it is reproduced here
directly — by making the savepoint recovery attempt itself fail — rather
than by faking a database outage a test can't otherwise manufacture.

**Why this lives in its own file, and why ``transaction=True``.** The
assertion depends on ``athlete_cell_write``'s own ``transaction.atomic()``
being the genuinely OUTERMOST transaction for the request — under the
default (non-transactional) ``django_db`` fixture, pytest-django wraps the
whole test in its own outer transaction first, so that atomic block would
actually be a nested savepoint instead, and the failure this test forces
would be absorbed one level higher instead of leaking out to a plain
``connection.rollback()`` the way it does in production. ``transaction=True``
removes that wrapper. This mechanism is pure Django transaction bookkeeping
(``connection.needs_rollback``), not a Postgres-specific abort — it
reproduces identically on SQLite, so this file (unlike the ``_postgres.py``
siblings) runs in the ordinary suite.
"""

import pytest
from django.db import connection
from django.db.utils import OperationalError

from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_failing_warn_read_does_not_roll_back_the_write(client, monkeypatch):
    """A dead-connection-class failure in the warn read must not cost the save."""
    s = seed()
    client.force_login(s.athlete)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated warn-read failure")

    def failing_savepoint_rollback(sid):
        # The "connection death" half of the review's chain: not just the
        # original read failing, but Django's OWN recovery attempt for it
        # ALSO failing — the only way `needs_rollback` survives an enclosing
        # savepoint. An ordinary single-statement failure does not do this
        # (Postgres's `ROLLBACK TO SAVEPOINT` succeeds even from an aborted
        # transaction), which is exactly why a milder failure wouldn't catch
        # the bug this test is for.
        raise OperationalError("simulated connection failure")

    monkeypatch.setattr("store_project.meso.views.sub_line_should_warn", boom)
    monkeypatch.setattr(connection, "savepoint_rollback", failing_savepoint_rollback)

    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    # The response is unaffected: a warn-read failure is never the athlete's
    # problem.
    assert resp.status_code == 200, "a warn-read failure must never 5xx"

    # The load-bearing assertions. If the warn read still shares (or
    # savepoints inside) the write's own transaction, this text and row are
    # gone — silently rolled back along with everything else in it.
    cell = sub_cell(s.squat, 1)
    assert cell.text == "225 x 5", (
        "the athlete's cell text was rolled back by a failed warn read -- "
        "_cell_warn_or_false must run OUTSIDE athlete_cell_write's write "
        "transaction, not inside it under a savepoint of its own"
    )
    assert LoggedSet.objects.filter(source_line=cell).exists(), (
        "the parsed LoggedSet was rolled back by a failed warn read"
    )
