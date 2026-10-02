"""#578 stage 4b — ``LoggedSet.reclaimed_line`` is gone from the model and every reader.

The DB column stays until stage 4c (old containers still select it during a deploy),
so the migration is state-only.
"""

import importlib
from pathlib import Path

from django.apps import apps
from django.db.migrations.loader import MigrationLoader

from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription

REPO_ROOT = Path(__file__).resolve().parents[4]
SKIP_DIRS = {
    "node_modules",
    "migrations",
    "dist",
    "build",
    "staticfiles",
    ".venv",
    "__pycache__",
    ".git",
}
SUFFIXES = {".py", ".html", ".js", ".ts", ".tsx", ".css", ".md"}
NEEDLE = "reclaimed"


def test_the_field_and_its_reverse_relation_are_gone():
    assert "reclaimed_line" not in {f.name for f in LoggedSet._meta.get_fields()}
    assert "reclaimed_sets" not in {f.name for f in Prescription._meta.get_fields()}


def test_no_source_line_mentions_the_retired_column():
    me = Path(__file__).resolve()
    hits = []
    for root in (REPO_ROOT / "app", REPO_ROOT / "frontend"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES or path == me:
                continue
            if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
                continue
            for n, line in enumerate(
                path.read_text(errors="ignore").splitlines(), start=1
            ):
                if NEEDLE in line.lower():
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{n}")
    assert not hits, "leftover references:\n" + "\n".join(hits)


def test_migration_state_drops_the_field_but_not_the_column():
    state = MigrationLoader(None).project_state()
    fields = state.models["meso", "loggedset"].fields
    assert "reclaimed_line" not in fields

    module = importlib.import_module(
        "store_project.meso.migrations.0059_loggedset_reclaimed_line_state_only"
    )
    (op,) = module.Migration.operations
    assert op.database_operations == []
    assert op.state_operations
    assert apps.get_app_config("meso")
