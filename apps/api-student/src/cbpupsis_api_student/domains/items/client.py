"""Internal client for the items domain — the extraction swap point.

Other domains depend on THIS module, never on ``service.py`` directly, and it
returns DTOs rather than ORM objects. If items is extracted into its own
service, only this file changes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.items import service
from cbpupsis_api_student.domains.items.schemas import ItemReadDetail


async def get_item(
    db: AsyncSession, item_id: uuid.UUID, viewer_id: uuid.UUID | None = None
) -> ItemReadDetail:
    """Return an item as a DTO. Raises NotFoundError if absent.

    ``viewer_id`` applies the same read boundary the HTTP endpoint does: items
    are private, so a viewer sees their own and, with ``ReadAllItem``, everyone's.

    It is optional because a caller may legitimately have no viewer — a
    background job reconciling data, or a domain acting on the system's behalf
    rather than a user's. **Pass it whenever the call is made on behalf of a
    user**, or this becomes the way one user's item reaches another: the
    boundary is enforced in the service, and omitting the argument opts out of
    it just as surely as removing the check would.
    """
    item = await service.get_item(db, item_id, viewer_id=viewer_id)
    return ItemReadDetail.model_validate(item)
    # After extraction, this becomes roughly:
    # resp = await http_client.get(f"{settings.items_service_url}/items/{item_id}")
    # return ItemReadDetail.model_validate(resp.json())
