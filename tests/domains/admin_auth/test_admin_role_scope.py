"""BR-AUTH-005 acceptance tests: Position-based Admin groups and role scoping.

Verifies:
1. Five groups seeded: Advisor, Chair, Dean, Registrar, and Finance.
2. Admin accounts belong to the group matching their position.
3. 403 AUTH_INSUFFICIENT_ROLE raised when a group lacks permission for a route.
4. Data scoping:
   - Chair reading another department: 404 RESOURCE_NOT_FOUND
   - Dean reading another college: 404 RESOURCE_NOT_FOUND
   - Advisor reading unassigned student: 404 RESOURCE_NOT_FOUND
   - Registrar and Finance have institution-wide scope
5. GET /api/v1/users/me returns group and page permissions.
6. BR-AUTH-005 segregation: Finance accounts cannot modify grades.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth import service as admin_service
from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminDataScope,
    AdminSession,
    require_admin_data_scope,
    require_admin_permission,
)
from cbpupsis_api_admin.routes import DEVELOPMENT_TOOLS
from cbpupsis_api_admin.routes import MOUNTS as ADMIN_MOUNTS
from cbpupsis_api_faculty.routes import MOUNTS as FACULTY_MOUNTS
from cbpupsis_api_student.routes import MOUNTS as STUDENT_MOUNTS
from cbpupsis_core.config import settings
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import AdminPosition, AdminProfile
from cbpupsis_database.session import get_db
from cbpupsis_shared.application import create_app
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import totp_at
from cbpupsis_shared.domains.grading.constants import MODERATE_GRADE
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import (
    ADMIN_GROUP,
    ADMIN_GROUPS,
    ADVISOR_GROUP,
    CHAIR_GROUP,
    DEAN_GROUP,
    FINANCE_GROUP,
    REGISTRAR_GROUP,
)
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.domains.users.constants import POSITION_GROUPS
from scripts import seed_iam

MANAGE_BILLING = "ManageBilling"
MODERATE_COURSE_SECTION = "ModerateCourseSection"
MODERATE_CURRICULUM = "ModerateCurriculum"
READ_ADVISING_RECORD = "ReadAdvisingRecord"

# Test router to verify HTTP dependency guards and data scoping
scope_router = APIRouter(prefix="/api/v1/test-admin-role-scope")


@scope_router.get("/billing")
async def get_billing_data(
    admin: AdminSession = Depends(require_admin_permission(MANAGE_BILLING)),
) -> dict[str, Any]:
    return {"status": "ok", "admin_id": str(admin.id), "action": "billing"}


@scope_router.post("/grades")
async def modify_grade(
    admin: AdminSession = Depends(require_admin_permission(MODERATE_GRADE)),
) -> dict[str, Any]:
    return {"status": "ok", "admin_id": str(admin.id), "action": "grade"}


@scope_router.get("/curriculum")
async def get_curriculum(
    admin: AdminSession = Depends(require_admin_permission(MODERATE_CURRICULUM)),
) -> dict[str, Any]:
    return {"status": "ok", "admin_id": str(admin.id), "action": "curriculum"}


@scope_router.get("/sections")
async def get_sections(
    admin: AdminSession = Depends(require_admin_permission(MODERATE_COURSE_SECTION)),
) -> dict[str, Any]:
    return {"status": "ok", "admin_id": str(admin.id), "action": "sections"}


@scope_router.get("/advising")
async def get_advising(
    admin: AdminSession = Depends(require_admin_permission(READ_ADVISING_RECORD)),
) -> dict[str, Any]:
    return {"status": "ok", "admin_id": str(admin.id), "action": "advising"}


@scope_router.get("/department/{dept_id}")
async def get_department_record(
    dept_id: str,
    scope: AdminDataScope = Depends(require_admin_data_scope),
) -> dict[str, Any]:
    scope.require_access(department_id=dept_id)
    return {"status": "ok", "department_id": dept_id}


@scope_router.get("/college/{college_id}")
async def get_college_record(
    college_id: str,
    scope: AdminDataScope = Depends(require_admin_data_scope),
) -> dict[str, Any]:
    scope.require_access(college_id=college_id)
    return {"status": "ok", "college_id": college_id}


@scope_router.get("/student-advising/{student_advisor_id}")
async def get_student_advising(
    student_advisor_id: uuid.UUID,
    scope: AdminDataScope = Depends(require_admin_data_scope),
) -> dict[str, Any]:
    scope.require_access(advisor_id=student_advisor_id)
    return {"status": "ok", "advisor_id": str(student_advisor_id)}


@pytest.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Test client with scope_router mounted without mutating main.app."""
    test_app = create_app(
        title=settings.app_name,
        mounts=(*STUDENT_MOUNTS, *FACULTY_MOUNTS, *ADMIN_MOUNTS),
        development_tools=DEVELOPMENT_TOOLS,
    )
    test_app.include_router(scope_router)
    test_app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as ac:
        yield ac


class _SessionWrapper:
    """Wrap test session so scripts calling AsyncSessionLocal() use test session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
async def seed_iam_environment(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.seed_iam.AsyncSessionLocal", lambda: _SessionWrapper(db)
    )
    await seed_iam.seed()


async def _create_and_login_admin(
    client: AsyncClient,
    db: AsyncSession,
    *,
    email: str,
    position: AdminPosition,
    department: str | None = None,
    college: str | None = None,
) -> tuple[uuid.UUID, str]:
    """Create verified admin user, set position/group, complete MFA.

    Returns (id, access_token).
    """
    user = await auth_service.create_console_account(
        db,
        email=email,
        password="Super-secure-password-42!",
        full_name=f"Admin {position.value.title()}",
    )
    await users_service.mark_email_verified(db, user.id)

    target_group = POSITION_GROUPS[position]
    group_id = await iam_service.get_group_id_by_name(db, target_group)
    assert group_id is not None, f"Group {target_group} was not found"
    await iam_service.add_user_to_group(db, user.id, group_id)

    await admin_service.configure_admin_profile(
        db,
        user_id=user.id,
        position=position,
        department_id=department,
        college_id=college,
    )

    # Login step 1: Password challenge
    resp = await client.post(
        "/api/v1/auth/admin/login",
        json={"email": email, "password": "Super-secure-password-42!"},
    )
    assert resp.status_code == 200, resp.text
    challenge = resp.json()

    # Login step 2: MFA enrollment + confirm
    enroll_resp = await client.post(
        "/api/v1/auth/admin/mfa/totp/enroll",
        json={"challenge_token": challenge["challenge_token"]},
    )
    assert enroll_resp.status_code == 200, enroll_resp.text
    secret = enroll_resp.json()["secret"]

    code = totp_at(secret, int(datetime.now(UTC).timestamp()) // 30)
    confirm_resp = await client.post(
        "/api/v1/auth/admin/mfa/totp/confirm",
        json={"challenge_token": challenge["challenge_token"], "code": code},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    token = confirm_resp.json()["access_token"]
    return user.id, token


class TestAdminGroupSeeding:
    """Verifies that seed_iam creates the five groups with their policies."""

    async def test_five_position_groups_exist_in_db(self, db: AsyncSession) -> None:
        groups = (await db.scalars(select(Group.name))).all()
        for expected in (
            ADVISOR_GROUP,
            CHAIR_GROUP,
            DEAN_GROUP,
            REGISTRAR_GROUP,
            FINANCE_GROUP,
        ):
            assert expected in groups, f"Expected group {expected} to be seeded"

    async def test_admin_groups_constant_matches_five_groups(self) -> None:
        assert {
            ADMIN_GROUP,
            ADVISOR_GROUP,
            CHAIR_GROUP,
            DEAN_GROUP,
            REGISTRAR_GROUP,
            FINANCE_GROUP,
        } == ADMIN_GROUPS

    async def test_seed_moves_existing_admins_into_their_position_group(
        self, db: AsyncSession
    ) -> None:
        """An Admin made before the position groups existed joins its group on seed."""
        user = await auth_service.create_console_account(
            db,
            email="existing_chair@example.com",
            password="A-valid-password-123!",
            full_name="Existing Chair",
        )
        db.add(
            AdminProfile(
                user_id=user.id,
                position=AdminPosition.chairperson.value,
                department="Computer Science",
            )
        )
        await db.flush()

        await seed_iam.seed()

        groups = await iam_service.get_user_group_names(db, user.id)
        assert POSITION_GROUPS[AdminPosition.chairperson] in groups


class TestBootstrapAdminPlacement:
    """Verifies scripts.bootstrap_admin places accounts in the position's group."""

    @pytest.mark.parametrize(
        ("position", "expected_group", "dept", "coll"),
        [
            (AdminPosition.advisor, ADVISOR_GROUP, None, None),
            (AdminPosition.chairperson, CHAIR_GROUP, "Computer Science", None),
            (AdminPosition.dean, DEAN_GROUP, None, "CCIS"),
            (AdminPosition.registrar, REGISTRAR_GROUP, None, None),
            (AdminPosition.finance, FINANCE_GROUP, None, None),
        ],
    )
    async def test_bootstrap_admin_assigns_position_group(
        self,
        db: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        position: AdminPosition,
        expected_group: str,
        dept: str | None,
        coll: str | None,
    ) -> None:
        user = await auth_service.create_console_account(
            db,
            email=f"bootstrap_{position.value}@example.com",
            password="A-valid-password-123!",
            full_name="Bootstrap Admin",
        )
        await users_service.mark_email_verified(db, user.id)

        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _SessionWrapper(db)
        )
        from scripts.bootstrap_admin import bootstrap_admin

        ret = await bootstrap_admin(
            user.email,
            position=position,
            department_id=dept,
            college_id=coll,
        )
        assert ret == 0

        groups = await iam_service.get_user_group_names(db, user.id)
        assert expected_group in groups


class TestAdminPermissionEnforcement:
    """Per-group 403 AUTH_INSUFFICIENT_ROLE assertions on routes lacking permissions."""

    async def test_advisor_cannot_modify_grades(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client, db, email="advisor@example.com", position=AdminPosition.advisor
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/test-admin-role-scope/grades", headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_chair_cannot_manage_billing(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client,
            db,
            email="chair@example.com",
            position=AdminPosition.chairperson,
            department="Computer Science",
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get(
            "/api/v1/test-admin-role-scope/billing", headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_dean_cannot_modify_grades(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client,
            db,
            email="dean@example.com",
            position=AdminPosition.dean,
            college="CCIS",
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/test-admin-role-scope/grades", headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_registrar_cannot_manage_billing(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client, db, email="registrar@example.com", position=AdminPosition.registrar
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get(
            "/api/v1/test-admin-role-scope/billing", headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_finance_cannot_modify_grades_br_auth_005(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Segregation from BR-AUTH-005: Finance account cannot change a grade."""
        _, token = await _create_and_login_admin(
            client, db, email="finance@example.com", position=AdminPosition.finance
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/test-admin-role-scope/grades", headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_each_group_can_access_its_own_permitted_route(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        # Advisor -> ReadAdvisingRecord
        _, adv_token = await _create_and_login_admin(
            client, db, email="adv1@example.com", position=AdminPosition.advisor
        )
        adv_resp = await client.get(
            "/api/v1/test-admin-role-scope/advising",
            headers={"Authorization": f"Bearer {adv_token}"},
        )
        assert adv_resp.status_code == 200

        # Chair -> ModerateCourseSection
        _, chair_token = await _create_and_login_admin(
            client,
            db,
            email="chair1@example.com",
            position=AdminPosition.chairperson,
            department="Computer Science",
        )
        chair_resp = await client.get(
            "/api/v1/test-admin-role-scope/sections",
            headers={"Authorization": f"Bearer {chair_token}"},
        )
        assert chair_resp.status_code == 200

        # Dean -> ModerateCurriculum
        _, dean_token = await _create_and_login_admin(
            client,
            db,
            email="dean1@example.com",
            position=AdminPosition.dean,
            college="CCIS",
        )
        dean_resp = await client.get(
            "/api/v1/test-admin-role-scope/curriculum",
            headers={"Authorization": f"Bearer {dean_token}"},
        )
        assert dean_resp.status_code == 200

        # Registrar -> ModerateGrade
        _, reg_token = await _create_and_login_admin(
            client, db, email="reg1@example.com", position=AdminPosition.registrar
        )
        reg_resp = await client.post(
            "/api/v1/test-admin-role-scope/grades",
            headers={"Authorization": f"Bearer {reg_token}"},
        )
        assert reg_resp.status_code == 200

        # Finance -> ManageBilling
        _, fin_token = await _create_and_login_admin(
            client, db, email="fin1@example.com", position=AdminPosition.finance
        )
        fin_resp = await client.get(
            "/api/v1/test-admin-role-scope/billing",
            headers={"Authorization": f"Bearer {fin_token}"},
        )
        assert fin_resp.status_code == 200


class TestDataScopeEnforcement:
    """Chair and Dean data scope filtering and 404 RESOURCE_NOT_FOUND."""

    async def test_chair_scope_own_department_succeeds_other_is_404(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client,
            db,
            email="chair_cs@example.com",
            position=AdminPosition.chairperson,
            department="Computer Science",
        )
        headers = {"Authorization": f"Bearer {token}"}

        # Own department -> 200 OK
        resp_own = await client.get(
            "/api/v1/test-admin-role-scope/department/Computer Science", headers=headers
        )
        assert resp_own.status_code == 200
        assert resp_own.json()["department_id"] == "Computer Science"

        # Other department -> 404 RESOURCE_NOT_FOUND
        resp_other = await client.get(
            "/api/v1/test-admin-role-scope/department/Electrical Engineering",
            headers=headers,
        )
        assert resp_other.status_code == 404
        assert resp_other.json()["code"] == "RESOURCE_NOT_FOUND"

    async def test_dean_scope_own_college_succeeds_other_is_404(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, token = await _create_and_login_admin(
            client,
            db,
            email="dean_ccis@example.com",
            position=AdminPosition.dean,
            college="CCIS",
        )
        headers = {"Authorization": f"Bearer {token}"}

        # Own college -> 200 OK
        resp_own = await client.get(
            "/api/v1/test-admin-role-scope/college/CCIS", headers=headers
        )
        assert resp_own.status_code == 200
        assert resp_own.json()["college_id"] == "CCIS"

        # Other college -> 404 RESOURCE_NOT_FOUND
        resp_other = await client.get(
            "/api/v1/test-admin-role-scope/college/CBA", headers=headers
        )
        assert resp_other.status_code == 404
        assert resp_other.json()["code"] == "RESOURCE_NOT_FOUND"

    async def test_advisor_scope_assigned_student_succeeds_other_is_404(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        advisor_id, token = await _create_and_login_admin(
            client,
            db,
            email="advisor_scope@example.com",
            position=AdminPosition.advisor,
        )
        headers = {"Authorization": f"Bearer {token}"}

        # Assigned student (advisor matches caller's user id) -> 200 OK
        resp_own = await client.get(
            f"/api/v1/test-admin-role-scope/student-advising/{advisor_id}",
            headers=headers,
        )
        assert resp_own.status_code == 200

        # Unassigned student -> 404 RESOURCE_NOT_FOUND
        other_advisor_id = uuid.uuid4()
        resp_other = await client.get(
            f"/api/v1/test-admin-role-scope/student-advising/{other_advisor_id}",
            headers=headers,
        )
        assert resp_other.status_code == 404
        assert resp_other.json()["code"] == "RESOURCE_NOT_FOUND"

    async def test_registrar_and_finance_have_institution_wide_scope(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        # Registrar accesses any department and college
        _, reg_token = await _create_and_login_admin(
            client, db, email="reg_scope@example.com", position=AdminPosition.registrar
        )
        reg_headers = {"Authorization": f"Bearer {reg_token}"}
        resp = await client.get(
            "/api/v1/test-admin-role-scope/department/AnyDepartment",
            headers=reg_headers,
        )
        assert resp.status_code == 200
        resp = await client.get(
            "/api/v1/test-admin-role-scope/college/AnyCollege", headers=reg_headers
        )
        assert resp.status_code == 200

        # Finance accesses any department and college
        _, fin_token = await _create_and_login_admin(
            client, db, email="fin_scope@example.com", position=AdminPosition.finance
        )
        fin_headers = {"Authorization": f"Bearer {fin_token}"}
        resp = await client.get(
            "/api/v1/test-admin-role-scope/department/AnyDepartment",
            headers=fin_headers,
        )
        assert resp.status_code == 200
        resp = await client.get(
            "/api/v1/test-admin-role-scope/college/AnyCollege", headers=fin_headers
        )
        assert resp.status_code == 200


class TestUsersMeReturnsGroupAndPagePermissions:
    """GET /api/v1/users/me returns group and page_permissions for all 5 roles."""

    @pytest.mark.parametrize(
        ("position", "expected_group", "expected_perm", "dept", "coll"),
        [
            (AdminPosition.advisor, ADVISOR_GROUP, READ_ADVISING_RECORD, None, None),
            (
                AdminPosition.chairperson,
                CHAIR_GROUP,
                MODERATE_COURSE_SECTION,
                "Computer Science",
                None,
            ),
            (AdminPosition.dean, DEAN_GROUP, MODERATE_CURRICULUM, None, "CCIS"),
            (AdminPosition.registrar, REGISTRAR_GROUP, MODERATE_GRADE, None, None),
            (AdminPosition.finance, FINANCE_GROUP, MANAGE_BILLING, None, None),
        ],
    )
    async def test_users_me_returns_group_and_permissions(
        self,
        client: AsyncClient,
        db: AsyncSession,
        position: AdminPosition,
        expected_group: str,
        expected_perm: str,
        dept: str | None,
        coll: str | None,
    ) -> None:
        _, token = await _create_and_login_admin(
            client,
            db,
            email=f"{position.value}_me@example.com",
            position=position,
            department=dept,
            college=coll,
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/users/me", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["group"] == expected_group
        assert expected_perm in body["page_permissions"]
        assert expected_perm in body["permissions"]
