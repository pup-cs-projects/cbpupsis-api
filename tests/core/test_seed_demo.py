"""Tests for the shared demo accounts: each must sign in to its own portal."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.auth.security import totp_at
from scripts import seed_demo, seed_iam

AUTH = "/api/v1/auth"


class _Session:
    """Wrap the test session so the script's `async with` does not close it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def _seed(db: AsyncSession, monkeypatch) -> None:
    for module in ("scripts.seed_iam", "scripts.seed_demo"):
        monkeypatch.setattr(f"{module}.AsyncSessionLocal", lambda: _Session(db))
    await seed_iam.seed()
    code = await seed_demo.seed_demo(
        seed_demo.DEFAULT_PASSWORD, seed_demo.DEFAULT_TOTP_SECRET
    )
    assert code == 0


def _code() -> str:
    step = int(datetime.now(UTC).timestamp()) // 30
    return totp_at(seed_demo.DEFAULT_TOTP_SECRET, step)


async def _verify(client: AsyncClient, path: str, challenge: dict) -> None:
    assert challenge["enrollment_required"] is False
    verified = await client.post(
        path,
        json={"challenge_token": challenge["challenge_token"], "code": _code()},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["access_token"]


async def test_every_demo_account_signs_in(
    db: AsyncSession, client: AsyncClient, monkeypatch
) -> None:
    await _seed(db, monkeypatch)
    password = seed_demo.DEFAULT_PASSWORD

    student = await client.post(
        f"{AUTH}/student/login",
        json={
            "student_number": "2026-00001-DM-0",
            "birthdate": "2005-01-15",
            "password": password,
        },
    )
    assert student.status_code == 200, student.text

    faculty = await client.post(
        f"{AUTH}/faculty/login",
        json={"employee_id": "2026-00002-DM-0", "password": password},
    )
    assert faculty.status_code == 200, faculty.text
    await _verify(client, f"{AUTH}/faculty/mfa/verify", faculty.json())

    admin = await client.post(
        f"{AUTH}/admin/login",
        json={"email": "demo-admin@example.com", "password": password},
    )
    assert admin.status_code == 200, admin.text
    await _verify(client, f"{AUTH}/admin/mfa/verify", admin.json())

    superadmin = await client.post(
        f"{AUTH}/superadmin/login",
        json={"email": "demo-superadmin@example.com", "password": password},
    )
    assert superadmin.status_code == 200, superadmin.text
    await _verify(client, f"{AUTH}/superadmin/mfa/verify", superadmin.json())


async def test_rerun_restores_a_changed_password(
    db: AsyncSession, client: AsyncClient, monkeypatch
) -> None:
    from sqlalchemy import select

    from cbpupsis_database.models.users import User

    await _seed(db, monkeypatch)
    user = await db.scalar(select(User).where(User.email == "demo-admin@example.com"))
    user.password_hash = "not-a-real-hash"
    await db.commit()

    await _seed(db, monkeypatch)
    admin = await client.post(
        f"{AUTH}/admin/login",
        json={
            "email": "demo-admin@example.com",
            "password": seed_demo.DEFAULT_PASSWORD,
        },
    )
    assert admin.status_code == 200, admin.text
