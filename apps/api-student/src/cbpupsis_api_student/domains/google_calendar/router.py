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
        400: {
            "description": "Invalid/incomplete term selectors, or "
            "GOOGLE_CALENDAR_INVALID_STATE for an expired, replayed, superseded "
            "or foreign-session OAuth attempt. Start fresh authorization."
        },
        401: {
            "description": "Missing, expired or invalid SIS student bearer session. "
            "Google credentials are not SIS access tokens."
        },
        403: {
            "description": "A live student role is required. All operations except "
            "disconnect also require ReadSchedule permission."
        },
        409: {
            "description": "GOOGLE_CALENDAR_CONFLICT: inspect connection status. "
            "Authorization conflicts with an existing connection/pending revocation; "
            "sync conflicts with a missing connection, active job or term limit."
        },
        503: {
            "description": "GOOGLE_CALENDAR_UNAVAILABLE: integration is disabled, "
            "provider authorization/validation failed or the callback timed out. "
            "Retry-After: 30. A failed callback needs fresh authorization, "
            "not replay of the same code/state."
        },
    },
)
PERMISSION = [Depends(require_permission("ReadSchedule"))]
Student = Annotated[CurrentUser, Depends(require_student_session)]
Database = Annotated[AsyncSession, Depends(get_db)]
Provider = Annotated[GoogleProvider, Depends(get_provider)]


@router.get(
    "",
    response_model=ConnectionRead,
    dependencies=PERMISSION,
    summary="Read Google Calendar Connection",
)
async def read_connection(user: Student, db: Database):
    """Inspect the signed-in student's Google connection without starting a sync.

    **Access:** live SIS student bearer session and `ReadSchedule` permission.
    Response is a direct snake_case object with `Cache-Control: private, no-store`.
    No Google credentials, account identity or calendar IDs are returned.

    ### Interpret the status
    - `enabled: false`: this optional integration is disabled on the deployment.
    - `connected`: a saved grant exists and is not marked `reconnectRequired`;
      this alone does not prove successful delivery.
    - `disconnecting`: Google token revocation is pending.
    - `last_sync_at`: last successful worker reconciliation, including unchanged
      checks. `next_sync_at` is a scheduled attempt, not a completion guarantee.
    - `targets[].provisioned`: a calendar was assigned, not proof of event delivery.

    Poll this endpoint after callback, sync or disconnect. Inspect `last_error`
    and verify the actual schedule in Google Calendar. After explicit disconnect,
    responses expose `targets: []`, even when private reconnect mappings remain.
    """
    return await service.status(db, user.id)


@router.post(
    "/authorization",
    response_model=AuthorizationRead,
    dependencies=PERMISSION,
    summary="Begin Google Calendar Authorization",
)
async def begin_authorization(selection: TermSelection, user: Student, db: Database):
    """Create a short-lived Google consent attempt for the signed-in student.

    **Access:** live SIS student bearer session and `ReadSchedule`. Google consent
    connects a calendar; it does not replace the SIS login.

    ### Request body
    Send `{}` for the current configured term, or both selectors:
    `{"year":"2026-2027","semester":"first"}`. Use the deployment's stored
    values; do not send blank strings or only one selector.

    ### Scalar and browser workflow
    1. Send this request in Scalar with the student's `access_token`.
    2. Open the returned `authorization_url` in a browser and select the requested
       app-created Calendar permission. Scalar does not replace Google consent.
    3. Google redirects to the configured frontend URI, **not** the backend POST
       callback. Forward the new `state` and `code` to `/callback` using the
       **same SIS session**, within `expires_in` (600 seconds).

    PowerShell is not required. State is single-use, protected by PKCE/OIDC nonce,
    and a new authorization supersedes the previous attempt. Never share the URL.
    Existing connections or pending revocations return 409; use `/sync` to add a
    term when connected. Operators must configure Google settings and run the
    worker for background synchronization.
    """
    return await service.authorize(db, user, selection)


@router.post(
    "/callback",
    response_model=ConnectionRead,
    dependencies=PERMISSION,
    summary="Complete Google Calendar Authorization",
)
async def complete_authorization(
    body: OAuthCallback, user: Student, db: Database, provider: Provider
):
    """Exchange a fresh Google browser redirect for an encrypted calendar grant.

    **Access:** the same live SIS student bearer session that began authorization,
    plus `ReadSchedule`. This is an authenticated POST, not Google's browser
    redirect destination.

    ### Scalar request
    From the **new** redirect URL, extract `state` and `code`, URL-decoding each
    value exactly once locally. Send `{"state":"...","code":"..."}`; do not
    paste the whole URL or use an online decoder. A missing local frontend page
    may show connection refused while its address bar still contains the values.
    Complete within ten minutes; never expose codes/state in logs or screenshots.

    ### Result and recovery
    200 means the grant was saved and initial sync queued, **not** that events
    arrived. Poll GET connection status, then inspect Google Calendar. The worker
    must be running. Same-account reconnect reuses known app-created mappings;
    a different verified Google account never receives the previous mappings.

    Expired, replayed, superseded or foreign-session attempts return 400. Google
    exchange/verification failures return 503. Start fresh authorization after
    either failure; the consumed callback must not be replayed. Ensure the
    requested Calendar permission was granted; no credentials are returned.
    """
    try:
        async with asyncio.timeout(25):
            return await service.callback(db, user, body, provider)
    except TimeoutError:
        await db.rollback()
        raise CalendarUnavailable from None


@router.post(
    "/sync",
    response_model=ConnectionRead,
    status_code=202,
    dependencies=PERMISSION,
    summary="Request Google Calendar Synchronization",
)
async def request_synchronization(
    selection: TermSelection, user: Student, db: Database
):
    """Queue one-way reconciliation of a student's SIS timetable into Google.

    **Access:** live SIS student bearer session, `ReadSchedule`, and a connected
    Google account. Send `{}` for the current term, or both `year` and `semester`
    using the stored identifiers. Up to ten terms may be pinned; no silent term
    rollover overwrites a prior semester.

    ### What is synchronized
    Forces reconciliation of app-created calendar series with committed SIS
    enrollment, meeting details, term bounds and configured holiday exclusions.
    Managed Google fields may be overwritten; Google edits never change SIS.
    No primary-calendar access, enrollment editing or approval APIs are involved.

    ### Verify completion
    202 means **queued**. The returned `last_sync_at` may refer to an earlier job,
    and `last_error: null` alone is not proof of completion. Allow worker polling/
    backlog, poll GET for a successful reconciliation, then inspect the calendar.
    Repeating unchanged sync must not create duplicate calendars/meeting series.

    Missing/revoking connections, active worker leases or the term limit return
    409. Inspect GET status before retrying. There is no immediate-delivery SLA.
    """
    return await service.queue_sync(db, user.id, selection)


@router.delete(
    "",
    response_model=ConnectionRead,
    status_code=202,
    summary="Disconnect Google Calendar",
)
async def disconnect_calendar(user: Student, db: Database):
    """Withdraw consent without deleting the student's Google calendar or events.

    **Access:** a live SIS student bearer session. Unlike the other Google
    operations, disconnect remains available after `ReadSchedule` is withdrawn.
    No request body or Google account/calendar identifier is required.

    ### Result and completion
    202 stops local sync immediately: `connected: false` and `targets: []`.
    `disconnecting: true` means token revocation is queued, not yet finished.
    Keep the worker running and poll GET until `disconnecting: false` and
    `next_sync_at: null`; inspect errors if revocation remains pending.
    After disconnect, `/sync` returns 409 until a fresh connection is completed.

    ### Retention and reconnect
    Existing Google calendars/events remain visible. Only private non-credential
    mappings are retained for same-account reuse and hidden from disconnected
    responses. An already in-flight provider request may finish. Wait for pending
    revocation, then start new authorization/consent with the same Google account
    to reconnect. Repeating DELETE is safe: it does not delete calendars, create
    a new grant or require the user to run PowerShell.
    """
    return await service.disconnect(db, user.id)
