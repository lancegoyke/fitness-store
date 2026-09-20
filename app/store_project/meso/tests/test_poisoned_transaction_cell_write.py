"""#571: ``athlete_cell_write`` must never claim a save the database didn't keep.

``_upsert_parsed_set`` wraps its work in a nested savepoint and swallows every
exception, on the reasoning that a parse or database problem inside it must
never turn a blur into a 4xx/5xx or lose the athlete's already-committed cell
text (parse-at-commit plan §11). That holds for a failure *raised inside* the
savepoint. It does not hold when the savepoint's OWN rollback also fails — a
dropped connection, a pgbouncer ``server_lifetime`` cycle, a mid-request DB
restart — which is exactly when Django leaves ``connection.needs_rollback``
set in a way no enclosing savepoint absorbs. Two shapes follow, depending on
what runs next inside ``athlete_cell_write``'s own outer atomic:

  1. A query follows the swallow — the ``SessionLog...update()`` activity
     bump — and raises ``TransactionManagementError`` on unfixed code: a 500
     that ALSO rolls back the already-committed ``cell.save()``.
  2. Nothing follows. The block exits with no exception and
     ``needs_rollback`` still set, so the outer atomic rolls back SILENTLY on
     unfixed code and the view answers 200 ``ok: True`` with a write that no
     longer exists.

Reuses the exact mechanism ``test_warn_read_transaction_placement.py`` pins
(read its module docstring for the full mechanics): ``transaction=True`` so
``athlete_cell_write``'s own ``transaction.atomic()`` is genuinely the
OUTERMOST transaction for the request — under the default, non-transactional
``django_db`` fixture, pytest-django's own outer transaction would absorb the
failure one level higher instead of leaking the way it does in production —
plus a monkeypatched ``savepoint_rollback`` that itself raises, the only way
``needs_rollback`` survives an enclosing savepoint.
"""

import pytest
from django.db import connection
from django.db.utils import OperationalError

from store_project.meso import views
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db(transaction=True)


def test_shape_1_a_query_after_the_swallow_is_kept_not_lost(client, monkeypatch):
    """A query following the swallowed failure must 503, not 500 or 200."""
    s = seed()
    client.force_login(s.athlete)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated parse failure")

    def failing_savepoint_rollback(sid):
        # The "connection death" half of the chain: not just the parse
        # failing, but Django's OWN recovery attempt for it ALSO failing —
        # the only way `needs_rollback` survives `_upsert_parsed_set`'s own
        # savepoint. An ordinary single-statement failure recovers fine
        # through `ROLLBACK TO SAVEPOINT`, even from an aborted transaction.
        raise OperationalError("simulated connection failure")

    monkeypatch.setattr("store_project.meso.views.parse_performed", boom)
    monkeypatch.setattr(connection, "savepoint_rollback", failing_savepoint_rollback)

    # A brand-new cell: `previous_text` is "" and this text is CHANGED, so
    # `text != previous_text` short-circuits the activity-bump `or` — the
    # query that then runs on unfixed code is the `SessionLog...update()`
    # bump, not `_line_sets` (which never gets a chance to be the culprit).
    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    assert resp.status_code == 503, (
        "a poisoned connection must answer 503 (the client keeps the write "
        "queued and retries), not 200 and not an escaping 500"
    )
    body = resp.json()
    assert body["ok"] is False

    # The fault is gone; a retry of the exact same write must land for real.
    # This is the load-bearing half of the guarantee: a 503 is only honest
    # if the text isn't actually lost, merely unconfirmed this one time.
    monkeypatch.undo()

    resp2 = write_cell(client, s.session, s.squat, 1, "225 x 5")
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    assert sub_cell(s.squat, 1).text == "225 x 5"


def test_shape_2_a_silent_post_swallow_rollback_is_still_answered_honestly(
    client, monkeypatch
):
    """Poisoning at the LAST query in the block, with nothing left to raise."""
    s = seed()
    client.force_login(s.athlete)

    # An athlete-authored line already carrying this exact text: `created_cell`
    # is False and `cell.athlete_authored` is True, so `untouched_coach_line`
    # is False regardless of whether the text matches — but the text posted
    # below WILL match, which is what lets the activity-bump `or` actually
    # reach `_line_sets(...)` instead of short-circuiting on it.
    setup = write_cell(client, s.session, s.squat, 1, "225 x 5")
    assert setup.status_code == 200

    real_line_sets = views._line_sets
    calls = []

    def fake_line_sets(session, athlete, cell):
        value = real_line_sets(session, athlete, cell)
        calls.append(value)
        if len(calls) == 2:
            # The SECOND call is the last statement `athlete_cell_write`
            # would otherwise run in the block: `text == previous_text`
            # below, so this is the query the activity-bump `or` falls
            # through to. Poison here, and return a value EQUAL to
            # `sets_before` (`calls[0]`) so the `if` condition is False
            # either way and the `.update()` never runs — nothing follows,
            # so on unfixed code there is nothing left to raise
            # `TransactionManagementError`; the block just exits and the
            # outer atomic rolls back SILENTLY underneath it.
            connection.needs_rollback = True
            return calls[0]
        return value

    monkeypatch.setattr(views, "_line_sets", fake_line_sets)

    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    assert resp.status_code == 503, (
        "unfixed code answers 200 ok:true here even though the block rolled "
        "back silently underneath it -- the write is lost and the athlete "
        "is told it saved"
    )
    assert resp.json()["ok"] is False
