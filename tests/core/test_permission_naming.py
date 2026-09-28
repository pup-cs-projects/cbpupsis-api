"""Tests that the permission vocabulary stays consistent.

Permission names are string literals in routers and services, so renaming one
after launch costs a code change and a data migration in lockstep. Drift is also
invisible: a codebase holding both ``ReadItem`` and ``ViewItem`` has a 403 in it
that no functional test catches, because each name looks correct on its own.

These tests are the guard. They fail when someone adds a permission outside the
fixed vocabulary, so the convention is enforced at the moment it is broken rather
than discovered later in production.

The vocabulary itself is documented in ``scripts/seed_iam.py`` and derived in the
API-GUIDE.md authorization notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.seed_iam import GROUPS, PERMISSIONS, POLICIES, RESERVED_POLICIES


#: Every workspace member's source, located by searching upward for the
#: workspace rather than counting parents, so moving this file cannot silently
#: point the scan at nothing — an ``rglob`` over a missing directory yields no
#: files and turns the scan below into a test that always passes.
def _source_dirs() -> list[Path]:
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages").is_dir() and (parent / "apps").is_dir():
            return sorted(
                d for base in ("packages", "apps") for d in parent.glob(f"{base}/*/src")
            )
    raise RuntimeError("could not locate the workspace root from this test file")


SOURCE_DIRS = _source_dirs()

#: Verbs acting on the caller's own records. Always paired with an ownership
#: check in the service layer.
OWN_VERBS = ("Create", "Read", "Update", "Delete")

#: Verbs that cross ownership. Each one IS the grant that lets a caller act on
#: records they do not own.
ELEVATED_VERBS = ("ReadAll", "Moderate", "Manage")

#: Real state transitions keep their business verb, because `UpdateRefund` would
#: lose the meaning that makes the permission worth having. Add sparingly: a
#: transition earns a permission only when it is granted separately from a plain
#: update.
TRANSITIONS: frozenset[str] = frozenset()

#: Words that mean one of the verbs above. Allowing a synonym is how a codebase
#: ends up with two names for one capability.
BANNED_SYNONYMS = (
    "View",
    "Get",
    "List",
    "Fetch",
    "Edit",
    "Modify",
    "Change",
    "Remove",
    "Archive",
    "Destroy",
    "Add",
    "Insert",
)

#: `ReadAll` must be tested before `Read`, or it is matched as `Read` + "AllItem".
_VERBS_LONGEST_FIRST = sorted(OWN_VERBS + ELEVATED_VERBS, key=len, reverse=True)

PERMISSION_ACTIONS = [action for action, _ in PERMISSIONS]


def split_verb(action: str) -> tuple[str, str] | None:
    """Split an action into (verb, noun), or None if no known verb matches."""
    for verb in _VERBS_LONGEST_FIRST:
        if action.startswith(verb) and len(action) > len(verb):
            return verb, action[len(verb) :]
    return None


class TestPermissionNames:
    @pytest.mark.parametrize("action", PERMISSION_ACTIONS)
    def test_uses_a_known_verb(self, action: str) -> None:
        """Every permission is VerbNoun from the fixed set, or a declared
        transition."""
        if action in TRANSITIONS:
            return
        assert split_verb(action) is not None, (
            f"{action!r} does not start with a known verb "
            f"{OWN_VERBS + ELEVATED_VERBS}. If it is a real state transition "
            f"(ApproveRefund, PublishListing), add it to TRANSITIONS here."
        )

    @pytest.mark.parametrize("action", PERMISSION_ACTIONS)
    def test_avoids_banned_synonyms(self, action: str) -> None:
        """No second name for a capability that already has one."""
        for synonym in BANNED_SYNONYMS:
            assert not action.startswith(synonym), (
                f"{action!r} starts with the banned synonym {synonym!r}. "
                f"Use one of {OWN_VERBS + ELEVATED_VERBS} instead."
            )

    @pytest.mark.parametrize("action", PERMISSION_ACTIONS)
    def test_noun_is_singular_pascal_case(self, action: str) -> None:
        """`ReadAllItem`, never `ReadAllItems` or `Read_all_items`."""
        if action in TRANSITIONS:
            return
        _verb, noun = split_verb(action)  # type: ignore[misc]
        assert noun[0].isupper(), f"{action!r}: noun {noun!r} must be PascalCase"
        assert "_" not in action, f"{action!r} must not contain underscores"
        # "Address"/"Status" end in s legitimately; "Items" does not.
        assert not (noun.endswith("s") and not noun.endswith(("ss", "us"))), (
            f"{action!r}: noun {noun!r} should be singular"
        )

    @pytest.mark.parametrize("action", PERMISSION_ACTIONS)
    def test_does_not_encode_the_group(self, action: str) -> None:
        """`AdminDeleteUser` defeats the indirection: the permission is
        `DeleteUser`, and only the admin group holds it."""
        for role_word in ("Admin", "Staff", "Owner", "Manager", "Super"):
            assert not action.startswith(role_word), (
                f"{action!r} encodes a role. Name the action, then grant it to "
                f"the group that should have it."
            )

    def test_no_duplicates(self) -> None:
        assert len(PERMISSION_ACTIONS) == len(set(PERMISSION_ACTIONS))


class TestPolicyNames:
    @pytest.mark.parametrize("policy", sorted(POLICIES))
    def test_follows_noun_tier(self, policy: str) -> None:
        """Policies are `<Noun><Tier>` — never a job title.

        `ManagerPolicy` is a group wearing a policy's clothes: the moment two
        roles share most of their permissions you cannot express it without
        duplicating.
        """
        assert policy.endswith(("Reader", "Author", "Moderator", "Admin")), (
            f"{policy!r} must end in Reader, Author, Moderator, or Admin"
        )
        assert not policy.endswith("Policy"), f"{policy!r}: drop the Policy suffix"


class TestModelIsCoherent:
    """Structural checks: a grant that leads nowhere reads as a capability."""

    def test_every_policy_permission_exists(self) -> None:
        known = set(PERMISSION_ACTIONS)
        for policy, actions in POLICIES.items():
            unknown = set(actions) - known
            assert not unknown, (
                f"policy {policy!r} references unknown {sorted(unknown)}"
            )

    def test_every_group_policy_exists(self) -> None:
        for group, policies in GROUPS.items():
            unknown = set(policies) - set(POLICIES)
            assert not unknown, f"group {group!r} references unknown {sorted(unknown)}"

    def test_every_permission_is_reachable(self) -> None:
        """A permission in no policy grants nothing but looks like a capability."""
        reachable = {a for actions in POLICIES.values() for a in actions}
        orphans = set(PERMISSION_ACTIONS) - reachable
        assert not orphans, f"permissions in no policy: {sorted(orphans)}"

    def test_every_policy_is_reachable(self) -> None:
        attached = {p for policies in GROUPS.values() for p in policies}
        orphans = set(POLICIES) - attached - RESERVED_POLICIES
        assert not orphans, f"policies in no group: {sorted(orphans)}"

    def test_elevated_permissions_are_not_universal(self) -> None:
        """No group may hold every elevated permission by default.

        A permission every group holds still gates something real: a user in NO
        group holds nothing, so `CreateItem` in every group is a fine way to say
        "any member may author items". What must never be universal is an
        elevated verb — if every group can act across ownership, the ordinary
        tier is decorative and the model has collapsed to one role.
        """
        per_group = [
            {a for p in policies for a in POLICIES[p]} for policies in GROUPS.values()
        ]
        universal = set.intersection(*per_group) if per_group else set()
        universal_elevated = {a for a in universal if a.startswith(ELEVATED_VERBS)}
        assert not universal_elevated, (
            f"every group crosses ownership via {sorted(universal_elevated)}, "
            f"so the ordinary tier grants nothing. Move it to the groups that "
            f"genuinely need it."
        )

    def test_a_baseline_group_exists(self) -> None:
        """The least-privileged group must hold no elevated permission.

        Someone has to represent "signed up, nothing special". Without one,
        every new user needs a privileged group to do anything at all, which is
        how default-admin ships.
        """
        by_size = sorted(
            GROUPS.items(),
            key=lambda kv: len({a for p in kv[1] for a in POLICIES[p]}),
        )
        name, policies = by_size[0]
        granted = {a for p in policies for a in POLICIES[p]}
        elevated = {a for a in granted if a.startswith(ELEVATED_VERBS)}
        assert not elevated, (
            f"the least-privileged group {name!r} already holds {sorted(elevated)}. "
            f"Add a baseline group with ordinary permissions only."
        )

    def test_admin_bootstrap_group_exists(self) -> None:
        """The Admin role exists without Superadmin-owned capabilities."""
        from scripts.bootstrap_admin import ADMIN_GROUP

        assert ADMIN_GROUP in GROUPS
        granted = {a for p in GROUPS[ADMIN_GROUP] for a in POLICIES[p]}
        assert (
            not {
                "ManageIAM",
                "ManageUser",
                "ReadAllUser",
                "ReadAllAuditEntry",
            }
            & granted
        )


class TestCodeMatchesSeed:
    """The seed is the source of truth; code must not reference a name it omits."""

    def test_every_enforced_permission_is_seeded(self) -> None:
        """A `require_permission("Typo")` is unreachable: no group can hold a
        permission that was never created, so the endpoint 403s for everyone."""
        known = set(PERMISSION_ACTIONS)
        pattern = re.compile(
            r'require_permission\(\s*"([A-Za-z]+)"'
            r'|_require_owner_or_permission\([^)]*?"([A-Za-z]+)"\s*\)'
        )
        missing: dict[str, str] = {}
        files = [p for d in SOURCE_DIRS for p in d.rglob("*.py")]
        assert files, "the scan found no source files"
        for path in files:
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                action = match.group(1) or match.group(2)
                if action and action not in known:
                    missing[action] = path.name
        assert not missing, (
            f"enforced but never seeded: {missing}. "
            f"Add them to PERMISSIONS in scripts/seed_iam.py."
        )
