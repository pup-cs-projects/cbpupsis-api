"""Fixed values for the Superadmin operations domain.

The permission names are also declared, independently, in
``scripts/seed_iam.py``; ``tests/domains/iam/test_permission_constants.py``
compares the two copies.
"""

from __future__ import annotations

MANAGE_OVERRIDE = "ManageOverride"
REQUEST_BACKUP_RESTORE = "RequestBackupRestore"
APPROVE_BACKUP_RESTORE = "ApproveBackupRestore"
