"""Seed baseline IAM permissions, policies, and groups idempotently.

Run on clean databases, during system bootstrap, and before tests that assert on
the authorization structure. Existing records are left intact: running this
twice produces the exact same database state.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from cbpupsis_database.models.iam import (
    Group,
    GroupPolicy,
    Permission,
    Policy,
    PolicyPermission,
    UserPolicy,
)
from cbpupsis_database.models.users import AdminProfile
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.iam.constants import (
    ADMIN_GROUP,
    ADMIN_POSITION_POLICIES,
    FACULTY_GROUP,
    STUDENT_GROUP,
    SUPERADMIN_GROUP,
)

#: VerbNoun permissions the application enforces. (action, description)
PERMISSIONS: list[tuple[str, str]] = [
    # Resources: items (a simple user-authored resource used for CRUD tests).
    ("CreateItem", "Create new items belonging to you"),
    ("ReadItem", "Read items you created"),
    ("UpdateItem", "Update items you created"),
    ("DeleteItem", "Delete items you created"),
    ("ReadAllItem", "Read items created by any user"),
    ("ModerateItem", "Update or delete items created by any user"),
    # Resources: users.
    ("CreateUser", "Provision new user accounts"),
    ("ReadAllUser", "List and read any user account"),
    ("ManageUser", "Deactivate or reactivate any user account"),
    ("ReadAllAuditEntry", "Read the administrative audit trail"),
    ("ReadAllNotification", "Read notifications belonging to anyone"),
    ("ManageNotificationDelivery", "Inspect and retry failed notification deliveries"),
    # Administering a resource itself, rather than its records.
    ("ManageIAM", "Manage permissions, policies, and groups"),
    ("ManageOverride", "Apply a justified Superadmin override to another account"),
    # Real transitions: requesting and approving a restore are separate grants,
    # because the dual-custody rule needs two different people to hold them.
    ("RequestBackupRestore", "Request that a database backup be restored"),
    ("ApproveBackupRestore", "Approve another Superadmin's restore request"),
    # Own records: a faculty member's assigned course sections and rosters.
    ("ReadCourseSection", "View the course sections assigned to you"),
    ("ReadGrade", "Read your own finalized grades"),
    ("ReadTranscript", "Read and export your own transcript"),
    ("ReadSchedule", "Read and export your own student course schedule"),
    ("CreateWaiverRequest", "File your own prerequisite waiver request"),
    ("ReadWaiverRequest", "Read position-scoped prerequisite waiver requests"),
    ("ApproveWaiverRequest", "Record the assigned prerequisite waiver decision"),
    ("IssueCertificate", "Issue your own Certificate of Registration"),
    ("ReadAssessment", "Read your own stored term assessment"),
    ("ReadEnrollmentReport", "Read enrollment counts and statistics report"),
    ("ReadEnrollment", "Read your own enrollment eligibility and unit ceiling"),
    ("ReadCatalog", "Read courses, curricula, and sections within your Admin scope"),
    (
        "ManageCatalog",
        "Create and update department courses, curriculum versions, and sections",
    ),
    (
        "ApproveSubstitution",
        "Approve course substitutions for students in your department",
    ),
    ("CreateEnrollment", "Submit your own enrollment section selections"),
    ("CreateOverloadRequest", "File your own enrollment overload request"),
    ("ReadOverloadRequest", "Read overload requests within your Admin scope"),
    ("ApproveOverloadRequest", "Approve or reject an in-scope overload request"),
    ("CreateAddDropRequest", "File your own add and drop request"),
    ("ReadAddDropRequest", "Read add and drop requests within your scope"),
    ("ApproveAddDrop", "Approve or reject an in-scope add and drop request"),
]

#: Policies as reusable bundles of the permissions above. name -> [actions]
POLICIES: dict[str, list[str]] = {
    "ItemAuthor": ["CreateItem", "ReadItem", "UpdateItem", "DeleteItem"],
    "ItemModerator": ["ReadAllItem", "ModerateItem"],
    "UserModerator": ["ReadAllUser"],
    "UserAdmin": ["CreateUser", "ReadAllUser", "ManageUser"],
    "IAMAdmin": ["ManageIAM"],
    "AuditReader": ["ReadAllAuditEntry"],
    "NotificationModerator": ["ReadAllNotification"],
    "NotificationAdmin": ["ReadAllNotification", "ManageNotificationDelivery"],
    "OverrideAdmin": ["ManageOverride"],
    "BackupAdmin": ["RequestBackupRestore", "ApproveBackupRestore"],
    "CourseSectionReader": ["ReadCourseSection"],
    "GradeReader": ["ReadGrade"],
    "TranscriptReader": ["ReadTranscript"],
    "ScheduleReader": ["ReadSchedule"],
    "WaiverRequestAuthor": ["CreateWaiverRequest"],
    "WaiverRequestAdmin": ["ReadWaiverRequest", "ApproveWaiverRequest"],
    "CertificateAuthor": ["IssueCertificate"],
    "AssessmentReader": ["ReadAssessment"],
    "EnrollmentReportReader": ["ReadEnrollmentReport"],
    "EnrollmentReader": ["ReadEnrollment"],
    "CatalogReader": ["ReadCatalog"],
    "CatalogAdmin": ["ReadCatalog", "ManageCatalog", "ApproveSubstitution"],
    "EnrollmentAuthor": ["CreateEnrollment"],
    "OverloadRequestAuthor": ["CreateOverloadRequest"],
    "OverloadRequestReader": ["ReadOverloadRequest"],
    "OverloadRequestAdmin": ["ReadOverloadRequest", "ApproveOverloadRequest"],
    "AddDropRequestAuthor": ["CreateAddDropRequest", "ReadAddDropRequest"],
    "AddDropRequestReader": ["ReadAddDropRequest"],
    "AddDropRequestAdmin": ["ReadAddDropRequest", "ApproveAddDrop"],
}

#: Policies reserved for the separate Superadmin role. They are seeded now
#: because their handlers already exist, but attaching them to the Admin role
#: would cross the product boundary for issue #91.
RESERVED_POLICIES: frozenset[str] = frozenset(
    {
        "UserAdmin",
        "IAMAdmin",
        "AuditReader",
        "NotificationAdmin",
        "OverrideAdmin",
        "BackupAdmin",
    }
)

#: Groups and the policies attached to each. name -> [policy names]
GROUPS: dict[str, list[str]] = {
    "Members": ["ItemAuthor"],
    "Moderators": [
        "ItemAuthor",
        "ItemModerator",
        "UserModerator",
        "NotificationModerator",
    ],
    # Admin is one authenticated role whose position controls data scope. Its
    # product permissions (Home, Calendar, Courses, Enrollment) will be attached
    # by those domains. User administration, IAM, and audit reading belong to
    # the separate Superadmin story and must not leak through this seed.
    ADMIN_GROUP: ["WaiverRequestAdmin"],
    FACULTY_GROUP: ["CourseSectionReader"],
    # Superadmin-created student accounts join this group.
    STUDENT_GROUP: [
        "GradeReader",
        "TranscriptReader",
        "ScheduleReader",
        "WaiverRequestAuthor",
        "CertificateAuthor",
        "AssessmentReader",
        "EnrollmentReader",
        "EnrollmentAuthor",
        "OverloadRequestAuthor",
        "AddDropRequestAuthor",
    ],
    SUPERADMIN_GROUP: list(POLICIES),
}


async def _get_or_create_permission(
    session, action: str, description: str
) -> Permission:
    """Return the permission with ``action``, creating it if absent."""
    existing = await session.scalar(
        select(Permission).where(Permission.action == action)
    )
    if existing is not None:
        return existing
    permission = Permission(action=action, description=description)
    session.add(permission)
    await session.flush()
    return permission


async def _get_or_create_policy(session, name: str) -> Policy:
    """Return the policy with ``name``, creating it if absent."""
    existing = await session.scalar(select(Policy).where(Policy.name == name))
    if existing is not None:
        return existing
    policy = Policy(name=name)
    session.add(policy)
    await session.flush()
    return policy


async def _ensure_policy_permission(
    session, policy_id: int, permission_id: int
) -> None:
    """Attach ``permission_id`` to ``policy_id`` if not already attached."""
    exists = await session.scalar(
        select(PolicyPermission).where(
            PolicyPermission.policy_id == policy_id,
            PolicyPermission.permission_id == permission_id,
        )
    )
    if exists is None:
        session.add(PolicyPermission(policy_id=policy_id, permission_id=permission_id))


async def _get_or_create_group(session, name: str) -> Group:
    """Return the group with ``name``, creating it if absent."""
    existing = await session.scalar(select(Group).where(Group.name == name))
    if existing is not None:
        return existing
    group = Group(name=name)
    session.add(group)
    await session.flush()
    return group


async def _ensure_group_policy(session, group_id: int, policy_id: int) -> None:
    """Attach ``policy_id`` to ``group_id`` if not already attached."""
    exists = await session.scalar(
        select(GroupPolicy).where(
            GroupPolicy.group_id == group_id,
            GroupPolicy.policy_id == policy_id,
        )
    )
    if exists is None:
        session.add(GroupPolicy(group_id=group_id, policy_id=policy_id))


async def _ensure_user_policy(session, user_id, policy_id: int) -> None:
    """Attach ``policy_id`` directly to ``user_id`` if it is absent."""
    exists = await session.scalar(
        select(UserPolicy).where(
            UserPolicy.user_id == user_id,
            UserPolicy.policy_id == policy_id,
        )
    )
    if exists is None:
        session.add(UserPolicy(user_id=user_id, policy_id=policy_id))


async def _seed_session(session) -> None:
    """Populate permissions, policies, groups, and admin policies on ``session``."""
    # 1) Permissions
    permissions: dict[str, Permission] = {}
    for action, description in PERMISSIONS:
        permissions[action] = await _get_or_create_permission(
            session, action, description
        )

    # 2) Policies, with their permissions attached
    policies: dict[str, Policy] = {}
    for policy_name, actions in POLICIES.items():
        policy = await _get_or_create_policy(session, policy_name)
        policies[policy_name] = policy
        for action in actions:
            await _ensure_policy_permission(session, policy.id, permissions[action].id)

    # 3) Groups, with their policies attached
    for group_name, policy_names in GROUPS.items():
        group = await _get_or_create_group(session, group_name)
        for policy_name in policy_names:
            await _ensure_group_policy(session, group.id, policies[policy_name].id)

    # 4) Position-specific policies for existing Admin accounts
    # Admin is one login role, but permissions differ by position.
    # Backfill direct policies for existing accounts; account creation uses
    # the same mapping for new accounts.
    admin_profiles = list((await session.scalars(select(AdminProfile))).all())
    for profile in admin_profiles:
        for policy_name in ADMIN_POSITION_POLICIES.get(profile.position, ()):
            await _ensure_user_policy(
                session, profile.user_id, policies[policy_name].id
            )

    await session.commit()


async def seed(session=None) -> None:
    """Create the baseline permissions, policies, and groups idempotently."""
    if session is not None:
        await _seed_session(session)
    else:
        async with AsyncSessionLocal() as new_session:
            await _seed_session(new_session)
    print("IAM seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
