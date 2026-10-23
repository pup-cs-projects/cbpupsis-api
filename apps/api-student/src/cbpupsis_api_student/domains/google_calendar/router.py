"""Student-only connection APIs; OAuth callback is forwarded by the client."""

import asyncio
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.google_calendar import service
from cbpupsis_api_student.domains.google_calendar.exceptions import CalendarUnavailable
from cbpupsis_api_student.domains.google_calendar.provider import GoogleProvider
from cbpupsis_api_student.domains.google_calendar.schemas import (
    AuthorizationRead,
    ConnectionRead,
    OAuthCallback,
    TermSelection,
)
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission


class PrivateCalendarRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            request.state.private_response = True
            request.state.redact_validation_errors = True
            return await original(request)

        return handle


async def get_provider():
    async with httpx.AsyncClient(
        timeout=8.0, follow_redirects=False, trust_env=False
    ) as client:
        yield GoogleProvider(client)


router = APIRouter(
    route_class=PrivateCalendarRoute,
    dependencies=[Depends(require_student_session)],
    responses={
        400: {"description": "Invalid term selectors or GOOGLE_CALENDAR_INVALID_STATE"},
        401: {"description": "A live student session is required"},
        403: {"description": "Student role or ReadSchedule permission is required"},
        409: {"description": "GOOGLE_CALENDAR_CONFLICT; inspect connection status"},
        503: {"description": "GOOGLE_CALENDAR_UNAVAILABLE; retry after 30 seconds"},
    },
)
PERMISSION = [Depends(require_permission("ReadSchedule"))]
Student = Annotated[CurrentUser, Depends(require_student_session)]
Database = Annotated[AsyncSession, Depends(get_db)]
Provider = Annotated[GoogleProvider, Depends(get_provider)]


@router.get("", response_model=ConnectionRead, dependencies=PERMISSION)
async def read_connection(user: Student, db: Database):
    """Read this student's optional Google Calendar connection and sync status.

    Requires a live student session and ReadSchedule. Responses are private,
    snake_case objects without Google credentials, account identity or calendar
    IDs. Disconnected connections expose no targets, even when private mappings
    are retained for reconnect. A queued request is not proof of Google delivery;
    inspect last_error and last_sync_at. Disabled integrations report enabled=false.
    """
    return await service.status(db, user.id)


@router.post(
    "/authorization", response_model=AuthorizationRead, dependencies=PERMISSION
)
async def begin_authorization(selection: TermSelection, user: Student, db: Database):
    """Begin Google consent without replacing the SIS student login.

    Send {} for the current configured term, or provide both year and semester.
    Open authorization_url in a browser. The single-use attempt expires after
    ten minutes and is bound to this SIS session using state, PKCE and OIDC nonce.
    Google redirects to the configured frontend URI, not the backend POST route.
    Already connected or revoking connections return 409; use /sync to add terms.
    """
    return await service.authorize(db, user, selection)


@router.post("/callback", response_model=ConnectionRead, dependencies=PERMISSION)
async def complete_authorization(
    body: OAuthCallback, user: Student, db: Database, provider: Provider
):
    """Complete browser consent using the same live SIS session.

    The frontend forwards the returned code/state as private JSON. Expired,
    replayed, superseded or foreign-session attempts return 400. Verified consent
    saves encrypted refresh credentials and queues initial synchronization; 200
    does not mean events have arrived. Reconnecting the same verified Google
    account reuses retained app-created mappings and reconciles managed fields.
    A different Google account never receives the previous account's mappings.
    """
    try:
        async with asyncio.timeout(25):
            return await service.callback(db, user, body, provider)
    except TimeoutError:
        await db.rollback()
        raise CalendarUnavailable from None


@router.post(
    "/sync", response_model=ConnectionRead, status_code=202, dependencies=PERMISSION
)
async def request_synchronization(
    selection: TermSelection, user: Student, db: Database
):
    """Queue reconciliation of a student's selected academic term.

    Send {} for the current term or both selectors for another configured term.
    Up to ten terms may be pinned. Returns 202 with status before background work
    completes; poll GET for errors/completion. Forces repair of missing or edited
    managed series, never imports Google changes into SIS. Requires a connection;
    disconnected/revoking or currently leased jobs return 409. No primary calendar
    access, student-number input or immediate-delivery guarantee is provided.
    """
    return await service.queue_sync(db, user.id, selection)


@router.delete("", response_model=ConnectionRead, status_code=202)
async def disconnect_calendar(user: Student, db: Database):
    """Stop local synchronization and queue Google authorization revocation.

    Requires a live student session, even when ReadSchedule has been withdrawn.
    Returns 202 immediately: connected becomes false and disconnecting remains
    true until the worker erases the pending revocation credential. Existing
    Google calendars/events are retained. Private, non-credential mappings remain
    for same-account reconnect but are hidden from disconnected responses. An
    already in-flight provider request may finish; fresh consent is required to
    reconnect. Repeating disconnect does not delete calendars or start a new grant.
    """
    return await service.disconnect(db, user.id)
