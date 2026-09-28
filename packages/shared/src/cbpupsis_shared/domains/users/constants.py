"""Fixed values for the users domain.

Column lengths are defined beside their columns in
``cbpupsis_database.models.users`` and re-exported here, so a schema validates
against the number the column holds: one admitting more would be a 500 at INSERT
rather than a validation error.
"""

from __future__ import annotations

import re

from cbpupsis_database.models.users import (
    AVATAR_URL_MAX_LENGTH as AVATAR_URL_MAX_LENGTH,
)
from cbpupsis_database.models.users import (
    DISPLAY_NAME_MAX_LENGTH as DISPLAY_NAME_MAX_LENGTH,
)
from cbpupsis_database.models.users import (
    FULL_NAME_MAX_LENGTH as FULL_NAME_MAX_LENGTH,
)
from cbpupsis_database.models.users import (
    LOCALE_MAX_LENGTH as LOCALE_MAX_LENGTH,
)
from cbpupsis_database.models.users import (
    PHONE_NUMBER_MAX_LENGTH as PHONE_NUMBER_MAX_LENGTH,
)
from cbpupsis_database.models.users import (
    TIMEZONE_MAX_LENGTH as TIMEZONE_MAX_LENGTH,
)

READ_ALL_USER = "ReadAllUser"
#: Administering accounts rather than records: deactivate and reactivate anyone.
#: Deliberately does NOT cover deletion, which stays self-service and
#: password-confirmed — see users/service.py::delete_own_account.
MANAGE_USER = "ManageUser"


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
