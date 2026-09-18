"""Fixed values for the audit domain.

Lengths are shared by models.py and schemas.py: a schema admitting more than the
column holds is a 500 at INSERT rather than a validation error.
"""

from __future__ import annotations

READ_ALL_AUDIT_ENTRY = "ReadAllAuditEntry"

ACTION_MAX_LENGTH = 100
TARGET_TYPE_MAX_LENGTH = 50

#: Event names this domain persists. Subscribing by explicit name rather than to
#: everything is the point: an audit trail is a deliberate record of
#: administrative change, not a firehose of every event the application emits.
#: Adding one here is the whole cost of auditing a new operation.
AUDITED_EVENTS: tuple[str, ...] = (
    "iam.policy_attached_to_group",
    "iam.policy_detached_from_group",
    "iam.user_added_to_group",
    "iam.user_removed_from_group",
    "iam.policy_attached_to_user",
    "iam.policy_detached_from_user",
    "iam.permission_created",
    "iam.policy_created",
    "iam.group_created",
    "user.deactivated",
    "user.reactivated",
)
