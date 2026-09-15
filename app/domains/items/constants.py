"""Fixed values for the items domain.

Permission actions are constants because a typo in a literal at a call site 403s
for every user, silently. As an import it fails at startup instead.

Lengths are shared by models.py and schemas.py: a schema admitting more than the
column holds is a 500 at INSERT.
"""

from __future__ import annotations

CREATE_ITEM = "CreateItem"
READ_ITEM = "ReadItem"
UPDATE_ITEM = "UpdateItem"
DELETE_ITEM = "DeleteItem"
READ_ALL_ITEM = "ReadAllItem"
MODERATE_ITEM = "ModerateItem"

NAME_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000

#: Numeric(12, 2): money is exact, never float.
PRICE_PRECISION = 12
PRICE_SCALE = 2
