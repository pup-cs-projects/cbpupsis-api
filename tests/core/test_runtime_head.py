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
