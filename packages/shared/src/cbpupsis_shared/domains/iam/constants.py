"""Fixed values for the IAM domain.

ADMIN_GROUP is imported by both seed_iam and bootstrap_admin: bootstrap looks up
the group the seed creates, so they must be the same string, not two equal ones.

Column lengths are defined beside their columns in
``cbpupsis_database.models.iam`` and re-exported here, so a schema validates
against the number the column holds: one admitting more would be a 500 at INSERT
rather than a validation error.
"""

from __future__ import annotations

from cbpupsis_database.models.iam import (
    ACTION_MAX_LENGTH as ACTION_MAX_LENGTH,
)
from cbpupsis_database.models.iam import (
    DESCRIPTION_MAX_LENGTH as DESCRIPTION_MAX_LENGTH,
)
from cbpupsis_database.models.iam import (
    NAME_MAX_LENGTH as NAME_MAX_LENGTH,
)

MANAGE_IAM = "ManageIAM"

#: The group bootstrap_admin promotes into. Must exist in seed_iam's GROUPS.
ADMIN_GROUP = "Admins"
SUPERADMIN_GROUP = "Superadmins"
#: Faculty accounts join this group; it carries their section-reading grant.
FACULTY_GROUP = "Faculty"
#: Student accounts join this group. It carries no policy yet: the student
#: domains attach theirs as they are built.
STUDENT_GROUP = "Students"

SUPERADMIN_ACTIONS = frozenset(
    {
        "CreateUser",
        "ReadAllUser",
        "ManageUser",
        "ManageIAM",
        "ReadAllAuditEntry",
        "ReadAllNotification",
        "ManageNotificationDelivery",
        "ManageOverride",
        "RequestBackupRestore",
        "ApproveBackupRestore",
    }
)
