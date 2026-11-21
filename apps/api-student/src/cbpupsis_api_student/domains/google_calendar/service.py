"""Consent, ownership and durable one-way reconciliation boundaries."""

import asyncio
import base64
import copy
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from cbpupsis_api_student.domains.google_calendar import repository, security
from cbpupsis_api_student.domains.google_calendar.exceptions import (
    CalendarConflict,
    CalendarUnavailable,
    InvalidOAuthState,
    LeaseLost,
    ProviderError,
)
from cbpupsis_api_student.domains.google_calendar.provider import SCOPE
from cbpupsis_api_student.domains.google_calendar.schemas import (
    AuthorizationRead,
    CalendarTargetRead,
    ConnectionRead,
)
from cbpupsis_api_student.domains.schedule import service as schedule_service
from cbpupsis_core.config import settings
from cbpupsis_core.exceptions import AppError
from cbpupsis_shared.domains.iam import service as iam
from cbpupsis_shared.domains.users import client as users

TERMINAL_ERRORS = {
    "reconnectRequired",
    "calendarCreationUncertain",
    "calendarMissing",
    "permissionDenied",
}


def now():
    return datetime.now(UTC)


def require_enabled():
    if not settings.google_calendar_enabled:
        raise CalendarUnavailable


def target_for(term):
    return {
        "year": term.year,
        "semester": term.semester,
        "calendarId": None,
        "provisioning": False,
        "events": {},
        "force": True,
    }


async def status(db, student_id):
    row = await repository.account(db, student_id)
    if row is None:
        return ConnectionRead(enabled=settings.google_calendar_enabled)
    return ConnectionRead(
        enabled=settings.google_calendar_enabled,
        connected=bool(row.refresh_cipher) and row.last_error != "reconnectRequired",
        disconnecting=bool(row.revoke_cipher),
        last_sync_at=row.last_sync_at,
        next_sync_at=row.next_sync_at,
        last_error=row.last_error,
        targets=[
            CalendarTargetRead(
                year=t["year"],
                semester=t["semester"],
                provisioned=bool(t["calendarId"]),
            )
            for t in row.targets.values()
            if row.refresh_cipher
        ],
    )


async def authorize(db, user, selection):
    require_enabled()
    if user.session_id is None:
        raise InvalidOAuthState
    term, _ = await schedule_service.calendar_snapshot(
        db, student_id=user.id, **selection.model_dump()
    )
    row = await repository.ensure_account(db, user.id)
    if row.revoke_cipher or (
        row.refresh_cipher and row.last_error != "reconnectRequired"
    ):
        raise CalendarConflict(
            "Already connected or disconnecting. Use sync for another term."
        )
    generation = uuid.uuid4()
    state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    payload = json.dumps(
        {
            "verifier": verifier,
            "nonce": nonce,
            "generation": str(generation),
            "termId": str(term.id),
            "target": target_for(term),
        }
    )
    await repository.save_account(db, user.id, {"generation": generation})
    await repository.replace_state(
        db,
        user.id,
        session_id=user.session_id,
        state_hash=security.digest(state),
        expires_at=now() + timedelta(minutes=10),
        payload_cipher=security.crypt(payload, user.id, "state"),
    )
    await db.commit()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return AuthorizationRead(
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(
            {
                "client_id": settings.google_calendar_client_id,
                "redirect_uri": settings.google_calendar_redirect_uri,
                "response_type": "code",
                "scope": f"openid {SCOPE}",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent select_account",
                "include_granted_scopes": "false",
            }
        )
    )


async def callback(db, user, body, provider):
    require_enabled()
    if user.session_id is None:
        raise InvalidOAuthState
    cipher = await repository.consume_state(
        db,
        user.id,
        user.session_id,
        security.digest(body.state.get_secret_value()),
        now(),
    )
    await db.commit()
    if cipher is None:
        raise InvalidOAuthState
    payload = json.loads(security.crypt(cipher, user.id, "state", decrypt=True))
    try:
        refresh, subject = await provider.exchange(
            body.code.get_secret_value(), payload["verifier"], payload["nonce"]
        )
    except ProviderError:
        raise CalendarUnavailable from None
    row = await repository.account(db, user.id, lock=True)
    if row is None or str(row.generation) != payload["generation"] or row.revoke_cipher:
        raise InvalidOAuthState
    subject_hash = security.digest(subject)
    targets = copy.deepcopy(row.targets) if row.subject_hash == subject_hash else {}
    targets.setdefault(payload["termId"], payload["target"])
    for target in targets.values():
        target["force"] = True
    if len(targets) > 10:
        raise CalendarConflict("A maximum of ten academic terms may be synchronized.")
    await repository.save_account(
        db,
        user.id,
        {
            "subject_hash": subject_hash,
            "refresh_cipher": security.crypt(refresh, user.id, "refresh"),
            "targets": targets,
            "last_sync_at": row.last_sync_at
            if row.subject_hash == subject_hash
            else None,
            "next_sync_at": now(),
            "last_error": None,
            "attempts": 0,
            "lease_id": None,
            "lease_until": None,
        },
    )
    await db.commit()
    return await status(db, user.id)


async def queue_sync(db, student_id, selection):
    require_enabled()
    term, _ = await schedule_service.calendar_snapshot(
        db, student_id=student_id, **selection.model_dump()
    )
    row = await repository.account(db, student_id, lock=True)
    if (
        row is None
        or not row.refresh_cipher
        or row.revoke_cipher
        or row.last_error == "reconnectRequired"
    ):
        raise CalendarConflict(
            "Connect Google Calendar before requesting synchronization."
        )
    if (
        row.lease_until
        and (
            row.lease_until.replace(tzinfo=UTC)
            if row.lease_until.tzinfo is None
            else row.lease_until
        )
        > now()
    ):
        raise CalendarConflict("Synchronization is already running. Retry shortly.")
    targets = copy.deepcopy(row.targets)
    targets.setdefault(str(term.id), target_for(term))["force"] = True
    if len(targets) > 10:
        raise CalendarConflict("A maximum of ten academic terms may be synchronized.")
    await repository.save_account(
        db,
        student_id,
        {"targets": targets, "next_sync_at": now(), "last_error": None, "attempts": 0},
    )
    await db.commit()
    return await status(db, student_id)


async def disconnect(db, student_id):
    row = await repository.account(db, student_id, lock=True)
    await repository.invalidate_state(db, student_id)
    if row is not None:
        await repository.save_account(
            db,
            student_id,
            {
                "generation": uuid.uuid4(),
                "revoke_cipher": row.refresh_cipher or row.revoke_cipher,
                "refresh_cipher": None,
                "lease_id": None,
                "lease_until": None,
                "next_sync_at": now()
                if row.refresh_cipher or row.revoke_cipher
                else None,
                "last_error": None,
                "attempts": 0,
            },
        )
    await db.commit()
    return await status(db, student_id)


def event_body(event):
    return {
        "summary": event["summary"],
        "description": event["description"],
        "location": event["location"],
        "status": "confirmed",
        "start": {"dateTime": event["start"], "timeZone": "Asia/Manila"},
        "end": {"dateTime": event["end"], "timeZone": "Asia/Manila"},
        "recurrence": [
            "RRULE:FREQ=WEEKLY;BYDAY="
            + event["weekday"]
            + ";UNTIL="
            + datetime.fromisoformat(event["until"]).strftime("%Y%m%dT%H%M%SZ"),
            *[
                "EXDATE:"
                + datetime.fromisoformat(value)
                .astimezone(UTC)
                .strftime("%Y%m%dT%H%M%SZ")
                for value in event["excluded"]
            ],
        ],
    }


def fingerprint(body):
    return security.digest(json.dumps(body, sort_keys=True))


async def checkpoint(db, student_id, lease, **values):
    if not await repository.save_account(
        db, student_id, values, lease=lease, now=now()
    ):
        await db.rollback()
        raise LeaseLost
    await db.commit()


async def reconcile(db, provider, student_id, lease, token, targets, target, events):
    if not target["calendarId"]:
        if target["provisioning"]:
            raise ProviderError("calendarCreationUncertain")
        target["provisioning"] = True
        await checkpoint(db, student_id, lease, targets=targets)
        try:
            target["calendarId"] = await provider.create_calendar(
                token, f"PUP Course Schedule {target['year']} {target['semester']}"
            )
        except ProviderError as exc:
            if exc.uncertain:
                raise ProviderError("calendarCreationUncertain") from None
            target["provisioning"] = False
            await checkpoint(db, student_id, lease, targets=targets)
            raise
        target["provisioning"] = False
        await checkpoint(db, student_id, lease, targets=targets)
    records = target["events"]
    if len(set(records) | set(events)) > 10000:
        raise ProviderError("sourceUnavailable")
    for uid, record in list(records.items()):
        if record.get("deleting") or (uid not in events and not record["deleted"]):
            record["deleting"] = True
            await checkpoint(db, student_id, lease, targets=targets)
            await provider.event(
                token, target["calendarId"], record["id"], method="DELETE"
            )
            record["deleted"] = True
            record["deleting"] = False
            await checkpoint(db, student_id, lease, targets=targets)
    for uid, event in events.items():
        body = event_body(event)
        desired = fingerprint(body)
        record = records.get(uid)
        if (
            record
            and not record["deleted"]
            and record["fingerprint"] == desired
            and not target.get("force")
        ):
            continue
        if record is None or record["deleted"]:
            revision = record["revision"] + 1 if record else 0
            record = {
                "revision": revision,
                "id": security.digest(f"{uid}:{revision}"),
                "fingerprint": None,
                "deleted": False,
            }
            records[uid] = record
        await checkpoint(db, student_id, lease, targets=targets)
        code, existing = await provider.event(token, target["calendarId"], record["id"])
        if code == 410 or existing.get("status") == "cancelled":
            record["revision"] += 1
            record["id"] = security.digest(f"{uid}:{record['revision']}")
            await checkpoint(db, student_id, lease, targets=targets)
            code, existing = await provider.event(
                token, target["calendarId"], record["id"]
            )
        if code == 404:
            await checkpoint(db, student_id, lease, targets=targets)
            await provider.insert_event(token, target["calendarId"], record["id"], body)
        elif code == 410:
            raise ProviderError("providerUnavailable")
        await checkpoint(db, student_id, lease, targets=targets)
        code, _ = await provider.event(
            token, target["calendarId"], record["id"], method="PUT", body=body
        )
        if code in {404, 410}:
            raise ProviderError("calendarMissing")
        record["fingerprint"] = desired
        await checkpoint(db, student_id, lease, targets=targets)
    target["force"] = False
    await checkpoint(db, student_id, lease, targets=targets)


async def run_due(db, provider):
    require_enabled()
    claimed = await repository.claim_due(db, now())
    if claimed is None:
        return False
    student_id, lease = claimed
    error = None
    attempts = 0
    revoking = False
    try:
        async with asyncio.timeout(90):
            row = await repository.account(db, student_id)
            targets, refresh_cipher, revoke_cipher = (
                copy.deepcopy(row.targets),
                row.refresh_cipher,
                row.revoke_cipher,
            )
            attempts = row.attempts
            revoking = bool(revoke_cipher)
            await db.rollback()
            if revoke_cipher:
                await checkpoint(db, student_id, lease, last_error=None)
                await provider.revoke(
                    security.crypt(revoke_cipher, student_id, "refresh", decrypt=True)
                )
                await checkpoint(
                    db,
                    student_id,
                    lease,
                    revoke_cipher=None,
                    next_sync_at=None,
                    lease_id=None,
                    lease_until=None,
                    last_error=None,
                )
                return True
            if await users.schedule_identity(
                db, student_id
            ) is None or "ReadSchedule" not in await iam.get_effective_permissions(
                db, student_id
            ):
                await db.rollback()
                await checkpoint(
                    db,
                    student_id,
                    lease,
                    refresh_cipher=None,
                    revoke_cipher=refresh_cipher,
                    next_sync_at=now(),
                    lease_id=None,
                    lease_until=None,
                )
                return True
            await db.rollback()
            snapshots = {}
            for term_id, target in targets.items():
                term, events = await schedule_service.calendar_snapshot(
                    db,
                    student_id=student_id,
                    year=target["year"],
                    semester=target["semester"],
                )
                if str(term.id) != term_id:
                    raise ProviderError("sourceUnavailable")
                snapshots[term_id] = events
            needs_sync = any(
                t.get("force")
                or not t["calendarId"]
                or any(
                    not t["events"].get(uid)
                    or t["events"][uid]["deleted"]
                    or t["events"][uid]["fingerprint"] != fingerprint(event_body(e))
                    for uid, e in snapshots[k].items()
                )
                or any(
                    r.get("deleting") or (uid not in snapshots[k] and not r["deleted"])
                    for uid, r in t["events"].items()
                )
                for k, t in targets.items()
            )
            if needs_sync:
                await checkpoint(db, student_id, lease, targets=targets)
                token, rotated = await provider.refresh(
                    security.crypt(refresh_cipher, student_id, "refresh", decrypt=True)
                )
                if rotated:
                    await checkpoint(
                        db,
                        student_id,
                        lease,
                        refresh_cipher=security.crypt(rotated, student_id, "refresh"),
                    )
                for term_id, target in targets.items():
                    await reconcile(
                        db,
                        provider,
                        student_id,
                        lease,
                        token,
                        targets,
                        target,
                        snapshots[term_id],
                    )
    except LeaseLost:
        return True
    except ProviderError as exc:
        error = exc.code
    except TimeoutError:
        error = "syncTimedOut"
    except AppError:
        error = "sourceUnavailable"
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception:
        error = "sourceUnavailable"
    await db.rollback()
    try:
        delay = (
            min(3600, 30 * 2 ** min(attempts, 7)) + secrets.randbelow(20)
            if error
            else settings.google_calendar_sync_seconds
        )
        await checkpoint(
            db,
            student_id,
            lease,
            last_error=error,
            attempts=attempts + 1 if error else 0,
            last_sync_at=now()
            if error is None
            else (await repository.account(db, student_id)).last_sync_at,
            next_sync_at=None
            if error in TERMINAL_ERRORS and not revoking
            else now() + timedelta(seconds=delay),
            lease_id=None,
            lease_until=None,
        )
    except LeaseLost:
        return True
    return True
