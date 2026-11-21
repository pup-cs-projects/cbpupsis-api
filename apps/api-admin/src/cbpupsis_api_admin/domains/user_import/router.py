"""HTTP layer for the CSV account import; mounted beneath /api/v1/users.

Served only by the admin app: it is the one image that parses multipart
uploads, and the only one a Superadmin signs in to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.user_import import service
from cbpupsis_api_admin.domains.user_import.schemas import UserImportResult
from cbpupsis_core.config import settings
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import (
    CurrentUser,
    require_superadmin_session,
)

router = APIRouter()


@router.post(
    "/import",
    response_model=UserImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_users(
    file: UploadFile = File(description="UTF-8 CSV with a header row"),
    user: CurrentUser = Depends(require_superadmin_session),
    db: AsyncSession = Depends(get_db),
) -> UserImportResult:
    """Create accounts from a CSV file, all or none. Superadmin only.

    422 ``USER_IMPORT_INVALID`` lists every problem by row and column when any
    row is invalid or collides with another row or an existing account; 413
    ``USER_IMPORT_TOO_LARGE`` past ``USER_IMPORT_MAX_BYTES``. Each created
    account is mailed its temporary password.
    """
    # One byte past the limit is enough to know it is too large.
    content = await file.read(settings.user_import_max_bytes + 1)
    return await service.import_accounts(db, actor_id=user.id, content=content)
