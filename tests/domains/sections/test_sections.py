"""US-102 section access (AC-102.6, AC-102.7): own sections only, 404 otherwise."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import FacultyProfile, User
from cbpupsis_shared.domains.auth.security import hash_password, totp_at

AUTH = "/api/v1/auth/faculty"
PASSWORD = "Faculty-Password-123!"


async def _faculty(db: AsyncSession, employee_id: str, email: str) -> User:
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(FacultyProfile(user_id=user.id, employee_id=employee_id, department="CS"))
    await db.commit()
    return user


async def _session(client: AsyncClient, employee_id: str) -> dict[str, str]:
    challenge = (
        await client.post(
            f"{AUTH}/login", json={"employee_id": employee_id, "password": PASSWORD}
        )
    ).json()["challenge_token"]
    secret = (
        await client.post(
            f"{AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
        )
    ).json()["secret"]
    confirmed = await client.post(
        f"{AUTH}/mfa/totp/confirm",
        json={
            "challenge_token": challenge,
            "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"Authorization": f"Bearer {confirmed.json()['access_token']}"}


def _section(faculty_id: uuid.UUID | None, name: str) -> CourseSection:
    return CourseSection(
        academic_term_id=uuid.uuid4(),
        course_id=uuid.uuid4(),
        faculty_id=faculty_id,
        section_name=name,
        capacity=40,
    )


@pytest.fixture
async def setup(db: AsyncSession, grant):
    mine = await _faculty(db, "2021-00456-MN-0", "mine@example.com")
    other = await _faculty(db, "2021-00457-MN-0", "other@example.com")
    await grant(mine.id, "ReadCourseSection")
    own = _section(mine.id, "BSCS 3-1")
    foreign = _section(other.id, "BSCS 3-2")
    db.add_all([own, foreign])
    await db.commit()
    return {"mine": mine, "other": other, "own": own, "foreign": foreign}


async def test_ac_102_6_lists_only_own_sections(client: AsyncClient, setup) -> None:
    headers = await _session(client, "2021-00456-MN-0")
    response = await client.get("/api/v1/sections", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [s["id"] for s in body["items"]] == [str(setup["own"].id)]


async def test_ac_102_7_someone_elses_section_answers_404(
    client: AsyncClient, setup
) -> None:
    headers = await _session(client, "2021-00456-MN-0")
    own = await client.get(
        f"/api/v1/sections/{setup['own'].id}/roster", headers=headers
    )
    assert own.status_code == 200 and own.json()["roster"] == []
    foreign = await client.get(
        f"/api/v1/sections/{setup['foreign'].id}/roster", headers=headers
    )
    missing = await client.get(
        f"/api/v1/sections/{uuid.uuid4()}/roster", headers=headers
    )
    assert foreign.status_code == missing.status_code == 404
    body_foreign, body_missing = foreign.json(), missing.json()
    body_foreign.pop("request_id")
    body_missing.pop("request_id")
    assert body_foreign == body_missing
    assert body_foreign["code"] == "RESOURCE_NOT_FOUND"


async def test_sections_need_the_permission(client: AsyncClient, setup) -> None:
    """The coarse check: a faculty session without ReadCourseSection is 403."""
    headers = await _session(client, "2021-00457-MN-0")
    assert (await client.get("/api/v1/sections", headers=headers)).status_code == 403
