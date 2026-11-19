"""The runtime gate's expected head must be the migration history's head.

Every API and worker refuses to start unless the database sits at
``EXPECTED_HEAD``. A revision added without bumping it makes every deployment
fail its startup check, which is how ``c004scratch`` went stale unnoticed.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from cbpupsis_database.runtime import EXPECTED_HEAD

MIGRATIONS = Path(__file__).resolve().parents[2] / "packages" / "migrations"


def test_expected_head_is_the_single_migration_head() -> None:
    config = Config(str(MIGRATIONS / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == [EXPECTED_HEAD]


def test_student_holds_runtime_acl_grants_required_crud() -> None:
    migration = (
        MIGRATIONS / "versions" / "c189_student_holds_runtime_acl.py"
    ).read_text(encoding="utf-8")
    assert (
        'f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE student_holds TO {quoted}"'
    ) in migration


def test_corrections_profile_merge_preserves_published_predecessors() -> None:
    config = Config(str(MIGRATIONS / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision("f103_grade_corrections").down_revision == (
        "f178_inc_lifecycle"
    )
    assert scripts.get_revision("f178_faculty_qualifications").down_revision == (
        "f178_inc_lifecycle"
    )
    assert set(scripts.get_revision("m103_corrections_profile").down_revision) == {
        "f103_grade_corrections",
        "f178_faculty_qualifications",
    }
