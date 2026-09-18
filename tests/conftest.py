"""Shared pytest fixtures.

Tests run against in-memory SQLite rather than Postgres so the suite needs no
running database and each test starts from a clean schema. That trade is
deliberate but not free: SQLite does not enforce every Postgres constraint
identically, so anything genuinely dialect-specific (JSONB operators, partial
indexes) deserves an integration test against a real Neon branch.

Environment defaults live in ``[tool.pytest.ini_options].env`` in pyproject.toml
(via pytest-env), because ``app.config`` validates settings at import time and
must see them before collection imports anything from ``app``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import every domain's models so Base.metadata is complete before create_all —
# the same footgun that migrations/env.py guards against.
from app.core.outbox import models as _outbox_models  # noqa: F401
from app.database import Base, get_db
from app.domains.audit import models as _audit_models  # noqa: F401
from app.domains.auth import models as _auth_models  # noqa: F401
from app.domains.auth import service as auth_service
from app.domains.iam import models as _iam_models  # noqa: F401
from app.domains.iam import service as iam_service
from app.domains.items import models as _items_models  # noqa: F401
from app.domains.notifications import models as _notifications_models  # noqa: F401
from app.domains.notifications.constants import NOTIFICATION_TYPES
from app.domains.users import models as _users_models  # noqa: F401
from app.domains.users import service as users_service
from app.main import app


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session against a fresh in-memory database.

    ``StaticPool`` keeps every connection pointed at the same in-memory
    database; without it each connection would get its own empty one.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Yield an HTTP client bound to the app, sharing the test's session."""
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def grant(db: AsyncSession):
    """Return a helper that grants a user one permission via a group.

    Exercises the real IAM path (permission -> policy -> group -> user) rather
    than inserting a shortcut row, so tests fail if that path breaks.
    """

    async def _grant(user_id: uuid.UUID, action: str) -> None:
        permission = await iam_service.create_permission(db, action=action)
        policy = await iam_service.create_policy(
            db, name=f"policy-{action}", permission_actions=[action]
        )
        group = await iam_service.create_group(db, name=f"group-{action}")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, user_id, group.id)
        assert permission.id is not None

    return _grant


@pytest.fixture
def make_user(db: AsyncSession):
    """Return a helper that registers a user through the auth service.

    The normal creation path, not a hand-built ORM row — a fixture that inserted
    directly would keep passing if ``register`` broke.

    Lives here rather than in one domain's tests because every domain's
    repository tests need a user to hang rows off: items need an owner, IAM
    needs a member, auth needs a token subject.
    """

    async def _make_user(email: str):
        return await auth_service.register(
            db, email=email, password="a-long-enough-password"
        )

    return _make_user


@pytest.fixture
def drain_outbox(db: AsyncSession):
    """Return a helper that delivers every pending outbox message, synchronously.

    The test-suite stand-in for the worker loop. It drives the **real**
    claim-and-dispatch path, so a test proves the production code rather than a
    parallel implementation of it — only the session and the handler's session
    are substituted, because the worker's own would reach an engine this suite
    cannot see.

    Needed wherever a side effect moved from "awaited inline in the request" to
    "staged durably and delivered later": the boundary is real in production, so
    making it explicit in the test is honest rather than inconvenient.
    """

    async def _drain() -> int:
        from app import worker
        from app.domains.notifications import handlers

        async def _handle(event, *, message_id):
            await handlers.deliver_event_on(db, event, message_id=message_id)

        previous = dict(worker._HANDLERS)
        worker._HANDLERS.clear()
        for name in NOTIFICATION_TYPES:
            worker.register_handler(name, _handle)
        try:
            return await worker.process_batch(db)
        finally:
            worker._HANDLERS.clear()
            worker._HANDLERS.update(previous)

    return _drain


@pytest.fixture
def sent_emails(monkeypatch) -> list[dict[str, str]]:
    """Capture outbound email instead of sending it, and expose what was sent.

    Patches ``send_email`` where the auth service *looked it up*, not where it
    is defined: the service imported the name at module load, so patching
    ``app.core.emails.send_email`` would leave that binding untouched.

    Tests use this to read the one-time token out of the message body, which is
    the only place it exists — by design, since only a digest is stored.
    """
    captured: list[dict[str, str]] = []

    async def _capture(*, to: str, subject: str, body: str) -> bool:
        captured.append({"to": to, "subject": subject, "body": body})
        return True

    # Patched at BOTH lookup sites, because outbound email now leaves by two
    # routes: auth still sends the one-time-token mails inline (they are the
    # flow, and a reset link must not arrive a poll interval late), while
    # everything durable goes through the notifications channel. Patching only
    # one would silently miss half the mail.
    monkeypatch.setattr("app.domains.auth.service.send_email", _capture)
    monkeypatch.setattr("app.domains.notifications.channels.send_email", _capture)
    return captured


def token_from_email(email: dict[str, str]) -> str:
    """Extract the one-time token from a captured message body."""
    return email["body"].split("token=")[1].split("\n")[0].strip()


@pytest.fixture
async def registered_user(client: AsyncClient, db: AsyncSession) -> dict[str, str]:
    """Register a **verified** user and return their credentials plus id.

    The email is marked verified because login requires it: an unverified
    account is refused with 403, so a fixture that skipped this step would hand
    every downstream test an account that cannot log in.

    Verification is stamped directly through the users service rather than by
    redeeming the mailed token, so this fixture does not depend on the
    ``sent_emails`` capture and stays usable by tests that never opt into it.
    The mailed-token path is exercised on its own in
    ``tests/test_auth_email_flows.py``.

    Tests that specifically need an *unverified* account register one inline
    rather than using this fixture.
    """
    payload = {
        "email": "user@example.com",
        "password": "correct-horse-battery-staple",
        "full_name": "Test User",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text

    user_id = response.json()["id"]
    await users_service.mark_email_verified(db, uuid.UUID(user_id))
    return {**payload, "id": user_id}


@pytest.fixture
async def auth_headers(
    client: AsyncClient, registered_user: dict[str, str]
) -> dict[str, str]:
    """Return Authorization headers for the registered user."""
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
