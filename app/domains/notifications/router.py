"""HTTP layer for the notifications domain.

Thin by design: endpoints parse, delegate, and shape the response.

**Note what is not here: a POST that creates a notification.** Nothing outside
the application may create one, because an endpoint that accepted a recipient
and a body would let any authenticated caller forge a message that appears to
come from the platform. Notifications arrive through the outbox.

**And note what carries no permission.** Reading and clearing your own
notifications is gated by authentication alone, the same way ``GET /users/me``
is: it is what having an account means, not a grantable capability. The service
scopes every query to the caller, so there is no id a client could supply to
reach somebody else's. Adding ``require_permission`` here would gate nothing and
imply the opposite.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.dependencies import CurrentUser, get_current_user
from app.domains.notifications import service as notifications_service
from app.domains.notifications.schemas import (
    NotificationPreferenceRead,
    NotificationPreferenceUpdate,
    NotificationRead,
    UnreadCount,
)
from app.shared.pagination import Page

router = APIRouter()


@router.get("", response_model=Page[NotificationRead])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Page[NotificationRead]:
    """List the caller's notifications, newest first."""
    page = await notifications_service.list_for_user(
        db, user.id, limit=limit, offset=offset
    )
    return Page[NotificationRead](
        items=[NotificationRead.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCount:
    """Return the caller's unread count — the bell badge.

    Polled on an interval by every client, so it is deliberately the cheapest
    endpoint here: one indexed count, no joins, no page of rows.
    """
    return UnreadCount(unread=await notifications_service.unread_count(db, user.id))


@router.get("/preferences", response_model=list[NotificationPreferenceRead])
async def get_preferences(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationPreferenceRead]:
    """Return the effective preference for every type and channel.

    Includes types the caller has never touched, with their defaults, so a
    settings screen can render the state the user is actually in.
    """
    return await notifications_service.get_preferences(db, user.id)


@router.put("/preferences", status_code=status.HTTP_204_NO_CONTENT)
async def set_preference(
    data: NotificationPreferenceUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Set one preference. 422 on an unknown type, or on silencing a notice."""
    await notifications_service.set_preference(
        db,
        user.id,
        notification_type=data.notification_type,
        channel=data.channel,
        enabled=data.enabled,
    )


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Mark every unread notification as read.

    Declared before ``/{notification_id}/read`` so the literal path is matched
    first — otherwise "read-all" is captured as a notification id and answers
    422 on the UUID parse.
    """
    await notifications_service.mark_all_read(db, user.id)


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_read(
    notification_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationRead:
    """Mark one notification as read.

    404 for somebody else's, not 403 — the service explains why.
    """
    notification = await notifications_service.mark_read(db, notification_id, user.id)
    return NotificationRead.model_validate(notification)
