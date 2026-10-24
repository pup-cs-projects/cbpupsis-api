"""The student_profile domain's service layer: this package, with its facade here.

Callers import ``cbpupsis_api_student.domains.student_profile.service``, never
an internal module inside it.
"""

from __future__ import annotations

from cbpupsis_api_student.domains.student_profile.service.avatar import (
    _AVATAR_STORE,
    get_avatar_bytes,
    inspect_image_content_type,
    scan_avatar_bytes,
    upload_avatar,
)
from cbpupsis_api_student.domains.student_profile.service.emergency_contacts import (
    create_emergency_contact,
    delete_emergency_contact,
    list_emergency_contacts,
    update_emergency_contact,
)
from cbpupsis_api_student.domains.student_profile.service.profile import (
    _apply_profile_field,
    get_profile,
    update_profile,
)

__all__ = [
    "_AVATAR_STORE",
    "_apply_profile_field",
    "create_emergency_contact",
    "delete_emergency_contact",
    "get_avatar_bytes",
    "get_profile",
    "inspect_image_content_type",
    "list_emergency_contacts",
    "scan_avatar_bytes",
    "update_emergency_contact",
    "update_profile",
    "upload_avatar",
]
