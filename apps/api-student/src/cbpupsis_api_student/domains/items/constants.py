"""Fixed values for the items domain.

Permission actions are constants because a typo in a literal at a call site 403s
for every user, silently. As an import it fails at startup instead.

Column lengths are defined beside their columns in
``cbpupsis_database.models.items`` and re-exported here, so a schema validates
against the number the column holds: one admitting more would be a 500 at INSERT
rather than a validation error.
"""

from __future__ import annotations

from cbpupsis_database.models.items import (
    DESCRIPTION_MAX_LENGTH as DESCRIPTION_MAX_LENGTH,
)
from cbpupsis_database.models.items import (
    NAME_MAX_LENGTH as NAME_MAX_LENGTH,
)
from cbpupsis_database.models.items import (
    PRICE_PRECISION as PRICE_PRECISION,
)
from cbpupsis_database.models.items import (
    PRICE_SCALE as PRICE_SCALE,
)

CREATE_ITEM = "CreateItem"
READ_ITEM = "ReadItem"
UPDATE_ITEM = "UpdateItem"
DELETE_ITEM = "DeleteItem"
READ_ALL_ITEM = "ReadAllItem"
MODERATE_ITEM = "ModerateItem"
