"""Issue #147: migrated routes preserve their guards and response contracts."""

from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader
from sqlalchemy import delete, select

from cbpupsis_api_student.domains.student_grades import service
from cbpupsis_api_student.main import app as student_app
from cbpupsis_database.models.auth import UserActiveSession
from cbpupsis_database.models.iam import PolicyPermission
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from main import app as composed_app
from tests.domains.schedule.helpers import NUMBER
from tests.domains.schedule.test_schedule import password_hash as password_hash
from tests.domains.schedule.test_schedule import setup as setup

RECORD_PATHS = (
    ("/api/v1/student/grades", "ReadGrade"),
    ("/api/v1/student/transcript", "ReadTranscript"),
    ("/api/v1/student/transcript/export", "ReadTranscript"),
)
REMOVED_OPERATIONS = (
    ("GET", "/api/v1/students/me/schedule"),
    ("GET", "/api/v1/students/me/schedule/export?format=pdf"),
    ("GET", "/api/v1/students/me/schedule/export?format=ics"),
    ("GET", "/api/v1/students/me/grades"),
    ("GET", "/api/v1/students/me/transcript"),
    ("GET", "/api/v1/students/me/transcript/export"),
    ("GET", f"/api/v1/students/{NUMBER}/schedule"),
    ("GET", f"/api/v1/students/{NUMBER}/grades"),
    ("GET", "/api/v1/students/me/schedule/google-calendar"),
    ("DELETE", "/api/v1/students/me/schedule/google-calendar"),
    ("POST", "/api/v1/students/me/schedule/google-calendar/authorization"),
    ("POST", "/api/v1/students/me/schedule/google-calendar/callback"),
    ("POST", "/api/v1/students/me/schedule/google-calendar/sync"),
)


@pytest.mark.parametrize(
    "target", [student_app, composed_app], ids=["student", "composed"]
)
@pytest.mark.parametrize("authenticated", [False, True])
async def test_every_removed_student_path_returns_404(db, setup, target, authenticated):
    """Replaces test_removed_schedule_aliases_return_404, including hidden routes."""
    previous = target.dependency_overrides.copy()
    target.dependency_overrides[get_db] = lambda: db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=target), base_url="http://test"
        ) as client:
            for method, path in REMOVED_OPERATIONS:
                response = await client.request(
                    method, path, headers=setup.headers if authenticated else {}
                )
                assert response.status_code == 404, (method, path, response.text)
                assert "location" not in response.headers
    finally:
        target.dependency_overrides.clear()
        target.dependency_overrides.update(previous)


async def test_ac_stu_004_8_removed_foreign_grades_route_returns_404(
    client, setup, db, password_hash
):
    foreign = User(email="foreign-grades@example.com", password_hash=password_hash)
    db.add(foreign)
    await db.flush()
    program_id = await db.scalar(
        select(StudentProfile.program_id).where(
            StudentProfile.user_id == setup.student_id
        )
    )
    db.add(
        StudentProfile(
            user_id=foreign.id,
            student_number="2021-00999-MN-1",
            program_id=program_id,
            year_level=1,
        )
    )
    await db.commit()
    for number in ("2021-00999-MN-1", "not-a-real-student"):
        response = await client.get(
            f"/api/v1/students/{number}/grades", headers=setup.headers
        )
        assert response.status_code == 404


@pytest.mark.parametrize(("path", "permission"), RECORD_PATHS)
async def test_migrated_record_routes_preserve_responses(
    client, setup, db, grant, path, permission
):
    await grant(setup.student_id, permission)
    response = await client.get(path, headers=setup.headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    if path.endswith("/export"):
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == (
            'attachment; filename="unofficial-transcript.pdf"'
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        text = "\n".join(
            page.extract_text() for page in PdfReader(BytesIO(response.content)).pages
        )
        assert NUMBER in text and "Unofficial" in text
    else:
        reader = (
            service.read_grades if path.endswith("/grades") else service.read_transcript
        )
        expected = await reader(db, student_id=setup.student_id)
        assert response.json() == expected.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(("path", "permission"), RECORD_PATHS)
async def test_migrated_record_routes_preserve_access_checks(
    client, setup, db, grant, path, permission
):
    assert (await client.get(path)).status_code == 401
    denied = await client.get(path, headers=setup.headers)
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "private, no-store"
    await grant(setup.student_id, permission)
    composed_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=setup.student_id, email="student@example.com", role="faculty"
    )
    assert (await client.get(path, headers=setup.headers)).status_code == 404
    composed_app.dependency_overrides.pop(get_current_user)
    await db.execute(delete(PolicyPermission))
    await db.commit()
    assert (await client.get(path, headers=setup.headers)).status_code == 403
    await db.execute(delete(UserActiveSession))
    await db.commit()
    assert (await client.get(path, headers=setup.headers)).status_code == 401
