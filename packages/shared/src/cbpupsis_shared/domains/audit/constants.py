"""Fixed values for the audit domain.

Column lengths live beside their columns in ``cbpupsis_database.models.audit``.
"""

from __future__ import annotations

READ_ALL_AUDIT_ENTRY = "ReadAllAuditEntry"


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


#: Which payload key identifies the thing acted upon, per event. The audit row
#: keeps a generic (target_type, target_id) pair so one index answers "what
#: happened to this?" regardless of which domain emitted it.
AUDIT_TARGETS: dict[str, tuple[str, str]] = {
    "iam.policy_attached_to_group": ("group", "group_id"),
    "iam.policy_detached_from_group": ("group", "group_id"),
    "iam.user_added_to_group": ("user", "user_id"),
    "iam.user_removed_from_group": ("user", "user_id"),
    "iam.policy_attached_to_user": ("user", "user_id"),
    "iam.policy_detached_from_user": ("user", "user_id"),
    "iam.permission_created": ("permission", "permission_id"),
    "iam.policy_created": ("policy", "policy_id"),
    "iam.group_created": ("group", "group_id"),
    "user.deactivated": ("user", "user_id"),
    "user.reactivated": ("user", "user_id"),
}
