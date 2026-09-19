r"""PostgreSQL-only: a failed ``track()`` insert must not poison its caller (#509).

``track()`` is called from inside write paths that hold an open transaction
(``athlete_cell_write``'s ``transaction.atomic()`` block, the batch deliver's
fan-out, ``invite_claim``'s locked accept). On PostgreSQL a failed statement
aborts the surrounding transaction; catching the exception doesn't clear
that, so every later query in the request fails and the outer block rolls
back the caller's own write. ``track()`` wraps its insert in its own
savepoint so a failure rolls back only the event.

**Why this file exists separately.** SQLite keeps the connection usable
after a failed statement, so these tests pass there whether or not the
savepoint exists: coverage that discriminates nothing. Same blind spot as
``meso/tests/test_parse_at_commit_postgres.py``.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \
        uv run pytest app/store_project/analytics/tests/test_track_postgres.py
"""

import pytest
from django.db import connection
from django.db import transaction

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.analytics.track import track
from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason=(
            "A failed statement only aborts the surrounding transaction on "
            "PostgreSQL; SQLite keeps the connection usable, so the savepoint's "
            "effect is invisible there."
        ),
    ),
]


@pytest.fixture
def failing_insert(monkeypatch):
    """Make every event insert run a genuinely invalid statement.

    A real failing query, not a hand-raised ``DatabaseError``: raising from
    Python leaves the connection healthy and reproduces nothing.
    """
    calls = []

    def boom(**_fields):
        calls.append(_fields)
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM a_table_that_does_not_exist")

    monkeypatch.setattr(Event.objects, "create", boom)
    return calls


def test_a_failed_insert_leaves_the_outer_transaction_usable(failing_insert):
    with transaction.atomic():
        before = UserFactory()
        track(EventName.PLAN_CREATED, actor=before)
        after = UserFactory()  # fails with "current transaction is aborted"

    assert failing_insert, "the insert was never attempted"
    assert User.objects.filter(pk__in=[before.pk, after.pk]).count() == 2


def test_a_failed_insert_does_not_cost_the_athlete_a_typed_set(client, failing_insert):
    """The typed-line path tracks ``set_logged`` inside the view's atomic block."""
    s = seed()
    client.force_login(s.athlete)

    resp = write_cell(client, s.session, s.squat, 1, "225 x 5")

    assert failing_insert, "set_logged was never tracked on the typed path"
    assert resp.status_code == 200
    cell = sub_cell(s.squat, 1)
    assert cell.text == "225 x 5"
    assert LoggedSet.objects.filter(source_line=cell).exists()
