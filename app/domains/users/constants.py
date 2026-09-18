"""Fixed values for the users domain.

Lengths are shared by models.py and schemas.py: a schema admitting more than the
column holds is a 500 at INSERT rather than a validation error.
"""

from __future__ import annotations

import re

READ_ALL_USER = "ReadAllUser"
#: Administering accounts rather than records: deactivate and reactivate anyone.
#: Deliberately does NOT cover deletion, which stays self-service and
#: password-confirmed — see users/service.py::delete_own_account.
MANAGE_USER = "ManageUser"

EMAIL_MAX_LENGTH = 320
FULL_NAME_MAX_LENGTH = 255
DISPLAY_NAME_MAX_LENGTH = 100
AVATAR_URL_MAX_LENGTH = 2048
TIMEZONE_MAX_LENGTH = 64
LOCALE_MAX_LENGTH = 35
PHONE_NUMBER_MAX_LENGTH = 20

#: E.164: a leading +, then 8-15 digits, first non-zero.
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
#: BCP 47 language tag, e.g. "en", "en-US", "zh-Hant-TW".
LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

#: Kept small on purpose: every field here stands between a new user and the
#: product. Extend it against a real requirement.
REQUIRED_ONBOARDING_FIELDS: tuple[str, ...] = ("display_name", "timezone")

#: Named explicitly so a new column on User is not editable by accident just
#: because a schema grew a matching field.
EDITABLE_PROFILE_FIELDS: frozenset[str] = frozenset(
    {
        "full_name",
        "display_name",
        "bio",
        "avatar_url",
        "timezone",
        "locale",
        "phone_number",
    }
)
