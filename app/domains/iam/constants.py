"""Fixed values for the IAM domain.

ADMIN_GROUP is imported by both seed_iam and bootstrap_admin: bootstrap looks up
the group the seed creates, so they must be the same string, not two equal ones.
"""

from __future__ import annotations

MANAGE_IAM = "ManageIAM"

#: The group bootstrap_admin promotes into. Must exist in seed_iam's GROUPS.
ADMIN_GROUP = "Admins"

ACTION_MAX_LENGTH = 100
NAME_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 255
