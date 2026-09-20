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

``transaction=True`` so ``athlete_cell_write``'s own ``transaction.atomic()``
is genuinely the OUTERMOST transaction for the request in both shapes below —
under the default, non-transactional ``django_db`` fixture, pytest-django's
own outer transaction would absorb the failure one level higher instead of
leaking the way it does in production.

The two shapes get ``needs_rollback`` set differently, on purpose. Shape 1
reuses the exact mechanism ``test_warn_read_transaction_placement.py`` pins
(read its module docstring for the full mechanics): a monkeypatched
``parse_performed`` that raises INSIDE ``_upsert_parsed_set``'s own savepoint,
plus a monkeypatched ``savepoint_rollback`` that itself raises — the only way
``needs_rollback`` survives that savepoint's own exit. Shape 2 sets
``connection.needs_rollback = True`` directly, from inside a monkeypatched
``_line_sets``, with no failing savepoint anywhere in the chain: it exists to
pin what happens AFTER the flag is already set with nothing left to run (the
"nothing follows" leg), not how the flag gets set, so re-deriving it through
shape 1's whole failing-savepoint dance would only obscure the part this
shape actually exercises.
"""

import pytest
from django.db import connection
from django.db.utils import OperationalError

from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db(transaction=True)


def test_shape_1_a_query_after_the_swallow_is_kept_not_lost(client, monkeypatch):
    """A query following the swallowed failure must 503, not 500 or 200."""
    s = seed()
    client.force_login(s.athlete)

    # A real prior write, so there is an OLD value on record that the
    # poisoned request below must not be able to overwrite — proving the
    # write was actually rolled back needs something for "rolled back" to
    # mean, and a brand-new cell has no prior state to revert to.
    setup = write_cell(client, s.session, s.squat, 1, "115 x 5")
    assert setup.status_code == 200

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

    # A genuine edit — DIFFERENT text than the cell already holds. It doesn't
    # have to be: `not connection.needs_rollback` sits FIRST in the
    # activity-bump `and` chain (set by the poisoned savepoint above, before
    # this condition is ever reached), so it short-circuits before `text !=
    # previous_text` is even consulted either way. A real edit is simply the
    # more realistic case to pin, and it's what makes the assertion below
    # meaningful.
    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    assert resp.status_code == 503, (
        "a poisoned connection must answer 503 (the client keeps the write "
        "queued and retries), not 200 and not an escaping 500"
    )
    body = resp.json()
    assert body["ok"] is False

    # The write was actually rolled back, not merely reported as unconfirmed.
    # `athlete_cell_write`'s own `transaction.atomic()` is the OUTERMOST
    # transaction here (`transaction=True`), so once `needs_rollback` survives
    # to its `__exit__` with no exception raised, Django rolls the WHOLE
    # block back on exit — `cell.save()`'s "225 x 5" included, even though
    # nothing here explicitly asked for that. A regression that answered 503
    # while letting the write commit anyway would leave the NEW text behind;
    # it must still read the one from setup.
    assert sub_cell(s.squat, 1).text == "115 x 5"

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
    original_set = LoggedSet.objects.get(source_line=sub_cell(s.squat, 1))

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

    # Reposts the SAME text as setup — required for `text != previous_text`
    # to be False so the activity-bump `or` actually reaches the second
    # `_line_sets(...)` call above rather than short-circuiting past it; see
    # the comment on the setup write. Because it's identical, `cell.text`
    # alone can't tell a commit that slipped through apart from a genuine
    # rollback (both read "225 x 5" either way) — that's checked below, but
    # the REAL discriminator is the row's identity, checked further down.
    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    assert resp.status_code == 503, (
        "unfixed code answers 200 ok:true here even though the block rolled "
        "back silently underneath it -- the write is lost and the athlete "
        "is told it saved"
    )
    assert resp.json()["ok"] is False
    assert sub_cell(s.squat, 1).text == "225 x 5"
    # `_upsert_parsed_set` always deletes-then-recreates the row it's
    # re-deriving from, even on a byte-identical repost (#567/#568's own
    # comment: "an unchanged re-blur ... delete-then-recreate[s] an EXISTING
    # performance, not a new one") — so a regression that let this commit
    # despite the 503 would leave a DIFFERENT row (a new pk) behind. The
    # ORIGINAL row surviving by identity is what actually proves the delete
    # never landed; `cell.text` above could not have told the two apart.
    assert LoggedSet.objects.filter(pk=original_set.pk).exists()

    # The fault is gone; a genuinely CHANGED write must still land — proving
    # the poisoned attempt above left the pipeline usable, not merely quiet.
    monkeypatch.undo()

    resp2 = write_cell(client, s.session, s.squat, 1, "315 x 3")
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    assert sub_cell(s.squat, 1).text == "315 x 3"
