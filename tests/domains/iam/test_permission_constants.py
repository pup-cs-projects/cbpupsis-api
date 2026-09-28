"""Tests that every permission constant names a permission the seed creates.

Constants closed one hole: a permission action misspelled at a call site is now
an ``ImportError`` at startup rather than an endpoint that 403s for everyone.
They do not close the other one. ``MODERATE_ITEM = "ModerateItm"`` imports
perfectly and reads correctly at every call site — but no group can hold an
action that ``scripts/seed_iam.py`` never created, so the guard denies every
caller, silently, exactly as the literal did.

That failure is invisible from either side alone: the seed looks complete, the
constants look complete, and only comparing them reveals the gap. These tests do
the comparison. ``tests/core/test_permission_naming.py`` covers the adjacent concern
— that the seeded names follow the fixed vocabulary — and its
``TestCodeMatchesSeed`` scans source for the string literals that used to be
there; between them, a permission is checked whether it is written as a literal
or as a name.

Deliberately asymmetric: every constant must be seeded, but not every seeded
permission needs a constant. ``ReadItem`` and ``ReadAllItem`` are seeded and
granted through policies without being enforced at any call site yet, which is a
legitimate state — the reverse never is.
"""

from __future__ import annotations

from types import ModuleType

import pytest

from cbpupsis_api_student.domains.items import constants as items_constants
from cbpupsis_shared.domains.auth import constants as auth_constants
from cbpupsis_shared.domains.iam import constants as iam_constants
from cbpupsis_shared.domains.notifications import constants as notifications_constants
from cbpupsis_shared.domains.users import constants as users_constants
from scripts.seed_iam import GROUPS, PERMISSIONS

#: Every domain's constants module. A new domain is added here, and its
#: permission constants are then covered automatically.
DOMAIN_CONSTANTS: dict[str, ModuleType] = {
    "auth": auth_constants,
    "users": users_constants,
    "iam": iam_constants,
    "items": items_constants,
    "notifications": notifications_constants,
}

SEEDED_ACTIONS = {action for action, _ in PERMISSIONS}

#: Names that are permission actions rather than any other kind of constant.
#: Identified by value, not by name: an action is a PascalCase string built from
#: the fixed verb vocabulary, which is what distinguishes it from a group name or
#: an error message living in the same module.
_ACTION_VERBS = ("Create", "Read", "Update", "Delete", "ReadAll", "Moderate", "Manage")


def _permission_constants(module: ModuleType) -> dict[str, str]:
    """Return the ``{constant_name: action}`` pairs a module declares."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper()
        and isinstance(value, str)
        and value.startswith(_ACTION_VERBS)
        # Group names are PascalCase too, so exclude anything the seed knows as
        # a group — ADMIN_GROUP = "Admins" would otherwise look like an action.
        and value not in GROUPS
    }


ALL_PERMISSION_CONSTANTS = [
    pytest.param(domain, name, action, id=f"{domain}.{name}")
    for domain, module in DOMAIN_CONSTANTS.items()
    for name, action in _permission_constants(module).items()
]


class TestPermissionConstantsAreSeeded:
    @pytest.mark.parametrize(("domain", "name", "action"), ALL_PERMISSION_CONSTANTS)
    def test_constant_is_seeded(self, domain: str, name: str, action: str) -> None:
        """A constant naming an unseeded permission still 403s for everyone."""
        assert action in SEEDED_ACTIONS, (
            f"{domain}/constants.py declares {name} = {action!r}, which "
            f"scripts/seed_iam.py never creates. No group can hold it, so every "
            f"endpoint guarding on it denies all callers. Add it to PERMISSIONS "
            f"in scripts/seed_iam.py, or correct the constant."
        )

    def test_the_check_is_not_vacuous(self) -> None:
        """Guards the guard: a broken detector would pass every test above.

        If ``_permission_constants`` stopped recognising actions — a renamed
        verb, a constant moved elsewhere — the parametrisation would collapse to
        nothing and this file would report success while checking nothing.
        """
        assert ALL_PERMISSION_CONSTANTS, (
            "no permission constants were discovered; the detection in "
            "_permission_constants is broken, not the constants"
        )

    def test_no_two_constants_share_an_action(self) -> None:
        """One action, one name. Two names for ``ModerateItem`` is the same
        drift the constants exist to prevent, moved up a level."""
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for domain, _name, action in (p.values for p in ALL_PERMISSION_CONSTANTS):
            if action in seen:
                duplicates.append(f"{action!r} in {seen[action]} and {domain}")
            seen[action] = domain
        assert not duplicates, f"an action is named twice: {duplicates}"


class TestAdminGroupIsShared:
    """The admin group name is one string two scripts must agree on."""

    def test_bootstrap_uses_the_iam_constant(self) -> None:
        """``bootstrap_admin`` looks the group up; ``seed_iam`` creates it. A
        drift between them means the bootstrap finds nothing and the first
        ``ManageIAM`` can never be granted."""
        from scripts.bootstrap_admin import ADMIN_GROUP

        assert ADMIN_GROUP is iam_constants.ADMIN_GROUP
        assert iam_constants.ADMIN_GROUP in GROUPS
