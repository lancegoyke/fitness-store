"""#691 — "set N" shown to a person is the ordinal among the exercise's logged sets.

A typed set's stored ``set_number`` is its cell LINE number, so a coach cue on line 1
pushes the athlete's first set to line 2 and "missed 3 reps on set 3" came out as
"set 4". Display surfaces number the logged sets 1..n instead; storage is unchanged.
"""

import pytest

from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.serializers import serialize_recent_logs
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def seed_cued_session(client):
    """``3 x 12`` with a coach ``RPE 7`` cue on line 1; the athlete logs lines 2-4."""
    s = seed()
    s.squat.text = "3 x 12"
    s.squat.save(update_fields=["text"])
    s.rdl.skipped = True
    s.rdl.save(update_fields=["skipped"])
    client.force_login(s.coach)
    assert reclaim(client, s, text="RPE 7", line=1).status_code == 200
    client.force_login(s.athlete)
    for line, text in ((2, "40 x 12"), (3, "40 x 12"), (4, "40 x 9")):
        assert write_cell(client, s.session, s.squat, line, text).status_code == 200
    assert log_post(client, s.session, {"status": "done"}).status_code == 200
    return s


def test_stored_set_numbers_are_the_line_numbers(client):
    seed_cued_session(client)
    numbers = sorted(LoggedSet.objects.values_list("set_number", flat=True))
    assert numbers == [2, 3, 4]


def test_results_note_names_the_third_set(client):
    s = seed_cued_session(client)
    rows = {r["name"]: r for r in presenters.session_results(s.session)["rows"]}
    assert rows["Box Squat"]["note"] == "missed 3 reps on set 3"


def test_athlete_readonly_row_reads_set_three(client):
    s = seed_cued_session(client)
    client.force_login(s.coach)
    assert reclaim(client, s, text="coach rewrote this", line=4).status_code == 200

    ctx = presenters.athlete_session(s.session, s.athlete)
    row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
    (shown,) = row["logged_readonly"]
    assert shown["label"].startswith("Set 3 ·"), shown["label"]


def test_recent_logs_number_sets_one_to_three(client):
    s = seed_cued_session(client)
    (log,) = serialize_recent_logs(s.plan)
    assert [x["set"] for x in log["sets"]] == [1, 2, 3]
