"""Focused #100 engineering evidence; fixtures are NOT #169/#99/#101 workflows."""

import base64
import os
import secrets
import uuid
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import SecretStr
from pypdf import PdfReader
from sqlalchemy import func, select, update

from cbpupsis_api_student.domains.schedule import cor_exports
from cbpupsis_api_student.main import app as student_app
from cbpupsis_core.config import settings
from cbpupsis_core.middleware.rate_limit import limiter
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import FeeStructure, StudentBillingAccount
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import (
    CorIssuance,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.users import StudentProfile
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.billing.assessment import AssessmentInputs
from cbpupsis_shared.domains.enrollment import academic_verdict, certificate
from cbpupsis_shared.domains.enrollment.certificate_security import crypt, token_hash
from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable
from main import app
from tests.domains.schedule.helpers import NUMBER, PASSWORD, seed_schedule

URL = "/api/v1/student/schedule/cor"
VERIFY = "/api/v1/public/cor/verify/"
REAL_VERDICT = academic_verdict.certificate_eligible


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
async def setup(db, client, grant, monkeypatch, password_hash):
    monkeypatch.setattr(
        settings,
        "cor_encryption_key",
        SecretStr(base64.urlsafe_b64encode(bytes(range(32))).decode()),
    )
    monkeypatch.setattr(
        settings, "frontend_base_url_student", "https://student.example.test"
    )

    async def synthetic_verdict(*args, **kwargs):
        return True

    # Explicit substitute for the absent #99 service, only in these unit tests.
    monkeypatch.setattr(academic_verdict, "certificate_eligible", synthetic_verdict)
    data = await seed_schedule(db, password_hash)
    await db.execute(
        update(TermEnrollment).values(enrollment_status="ENROLLED_PENDING")
    )
    profile = await db.get(StudentProfile, data.student_id)
    db.add(
        FeeStructure(
            program_id=profile.program_id,
            academic_term_id=data.term_id,
            tuition_per_unit=Decimal("100.25"),
            registration_fee=Decimal("50.00"),
            lab_fee=Decimal("25.00"),
            misc_fee=Decimal("10.00"),
        )
    )
    await db.flush()
    data.assessment = await billing.stage_assessment(
        db,
        student_id=data.student_id,
        term_id=data.term_id,
        courses=[
            billing.AssessmentCourse(section_id=s, units=Decimal(3))
            for s in data.section_ids
        ],
        due_date=date(2026, 12, 1),
    )
    await db.commit()
    for permission in ("IssueCertificate", "ReadAssessment", "ReadSchedule"):
        await grant(data.student_id, permission)
    response = await client.post(
        "/api/v1/auth/student/login",
        json={
            "student_number": NUMBER,
            "birthdate": "2003-05-14",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    data.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    return data


async def issue(client, setup):
    response = await client.post(
        URL, json={"academicTermId": str(setup.term_id)}, headers=setup.headers
    )
    assert response.status_code == 201, response.text
    return response


async def read(client, setup, format="json"):
    return await client.get(
        URL,
        params={"academicTermId": str(setup.term_id), "format": format},
        headers=setup.headers,
    )


async def test_ac_enrl_003_1_all_courses_immutable_json_and_pdf(client, setup, db):
    await issue(client, setup)
    before = await read(client, setup)
    dto = before.json()
    assert len(dto["courses"]) == 2 and Decimal(dto["totalUnits"]) == 6
    assert dto["program"] == "Computer Science" and dto["studentNumber"] == NUMBER
    assert all(
        c["section"] and c["schedule"] and c["room"] and c["faculty"]
        for c in dto["courses"]
    )
    await db.execute(update(Course).values(title="Changed catalogue", lecture_units=9))
    await db.commit()
    assert (await read(client, setup)).json() == dto
    response = await read(client, setup, "pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="certificate.pdf"'
    )
    text = " ".join(
        p.extract_text() for p in PdfReader(BytesIO(response.content)).pages
    )
    for value in (
        dto["name"],
        NUMBER,
        dto["program"],
        dto["certificateId"],
        dto["assessment"]["amountAssessed"],
        dto["verificationUrl"],
    ):
        assert value in text
    for course in dto["courses"]:
        assert course["code"] in text and course["section"] in text
    assert "Changed catalogue" not in text
    assert before.headers["cache-control"] == "private, no-store"


async def test_ac_enrl_003_2_shared_stored_assessment_full_subsidy(client, setup, db):
    response = await client.get(
        "/api/v1/student/assessment",
        params={"academicTermId": str(setup.term_id)},
        headers=setup.headers,
    )
    assert response.status_code == 200
    assessment = response.json()
    assert assessment["netPayable"] == "0.00"
    assert assessment["amountAssessed"] == assessment["subsidyApplied"] == "686.50"
    assert sum(Decimal(line["amount"]) for line in assessment["lines"]) == Decimal(
        "686.50"
    )
    await issue(client, setup)
    assert (await read(client, setup)).json()["assessment"] == assessment
    account = await db.scalar(select(StudentBillingAccount))
    assert account.assessment_snapshot["inputs"]["tuition_per_unit"] == "100.25"


async def test_ac_enrl_003_3_random_encrypted_recoverable_tokens(
    client, setup, db, monkeypatch
):
    generated = []
    real_random = secrets.token_urlsafe

    def random_token(size):
        assert size == 32
        value = real_random(size)
        generated.append(value)
        return value

    monkeypatch.setattr(certificate.secrets, "token_urlsafe", random_token)
    await issue(client, setup)
    row = await db.scalar(select(CorIssuance))
    assert row.verification_token_hash == token_hash(generated[0])
    assert crypt(row.token_envelope, row.id, "token", decrypt=True) == generated[0]
    assert generated[0] not in row.token_envelope + row.snapshot_envelope
    assert NUMBER not in row.snapshot_envelope and len(generated[0]) == 43
    before = (await read(client, setup)).json()
    monkeypatch.setattr(
        settings, "jwt_secret", SecretStr("different-jwt-key-never-the-cor-key-123456")
    )
    assert certificate._snapshot(row).verificationUrl == before["verificationUrl"]


async def test_ac_enrl_003_4_anonymous_verification_exact_fields(client, setup):
    await issue(client, setup)
    dto = (await read(client, setup)).json()
    token = dto["verificationUrl"].split("/")[-1]
    response = await client.get(VERIFY + token)
    assert response.status_code == 200
    assert set(response.json()) == {
        "name",
        "studentNumber",
        "term",
        "issuedAt",
        "status",
    }
    assert response.json()["status"] == "CURRENT"
    assert set(response.json()["term"]) == {"year", "semester"}
    assert "no-store" in response.headers["cache-control"]


async def test_ac_enrl_003_5_no_enrollment_refused_without_writes(client, setup, db):
    term = str(uuid.uuid4())
    for method, path, kwargs in (
        (client.post, URL, {"json": {"academicTermId": term}}),
        (client.get, URL, {"params": {"academicTermId": term}}),
        (
            client.get,
            "/api/v1/student/assessment",
            {"params": {"academicTermId": term}},
        ),
    ):
        response = await method(path, headers=setup.headers, **kwargs)
        assert (
            response.status_code == 409
            and response.json()["code"] == "COR_NOT_ENROLLED"
        )
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 0


@pytest.mark.parametrize("unmet", ["HOLD", "PREREQUISITE_VIOLATION", "NO_ASSESSMENT"])
async def test_ac_enrl_003_6_each_precondition_refused(
    client, setup, db, monkeypatch, unmet
):
    if unmet == "HOLD":
        await db.execute(update(StudentProfile).values(has_academic_hold=True))
    elif unmet == "NO_ASSESSMENT":
        await db.execute(update(StudentBillingAccount).values(assessment_snapshot=None))
    else:

        async def violated(*args, **kwargs):
            return False

        monkeypatch.setattr(academic_verdict, "certificate_eligible", violated)
    await db.commit()
    for response in (
        await client.post(
            URL, json={"academicTermId": str(setup.term_id)}, headers=setup.headers
        ),
        await read(client, setup),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "COR_ENROLLMENT_NOT_FINAL"
        assert unmet in response.json()["details"]["unmet"]
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 0


async def test_ac_enrl_003_7_live_session_permissions_and_no_student_selector(
    client, setup, db
):
    assert (
        await client.get(URL, params={"academicTermId": str(setup.term_id)})
    ).status_code == 401
    # Existing producer fixture has another live user but no student session.
    from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=setup.faculty_id, email="faculty@example.com", role="faculty"
    )
    assert (await read(client, setup)).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=setup.faculty_id, email="other@example.com", role="student"
    )
    assert (await read(client, setup)).status_code == 403  # No ReadSchedule grant.
    from cbpupsis_database.models.iam import Group
    from cbpupsis_shared.domains.iam import service as iam

    group = await db.scalar(select(Group).where(Group.name == "group-ReadSchedule"))
    await iam.add_user_to_group(db, setup.faculty_id, group.id)
    response = await client.get(
        URL,
        params={"academicTermId": str(setup.term_id), "studentNumber": NUMBER},
        headers=setup.headers,
    )
    assert response.status_code == 409 and response.json()["code"] == "COR_NOT_ENROLLED"
    del app.dependency_overrides[get_current_user]
    response = await client.post(
        URL,
        json={"academicTermId": str(setup.term_id), "studentNumber": "someone-else"},
        headers=setup.headers,
    )
    assert response.status_code == 422
    for route in student_app.routes:
        if "cor" in getattr(route, "path", ""):
            assert (
                "student_number" not in route.path and "studentNumber" not in route.path
            )


async def test_ac_enrl_003_8_internal_reissue_preserves_old_token_not_approval_evidence(
    client, setup, db
):
    await issue(client, setup)
    old = (await read(client, setup)).json()
    # #101 approval does not exist: test only changes + assessment + hook.
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    fresh = await billing.stage_assessment(
        db,
        student_id=setup.student_id,
        term_id=setup.term_id,
        courses=[
            billing.AssessmentCourse(section_id=setup.section_ids[1], units=Decimal(3))
        ],
        due_date=date(2026, 12, 1),
    )
    await certificate.stage_reissue(
        db,
        student_id=setup.student_id,
        term_id=setup.term_id,
        actor_id=setup.faculty_id,
        assessment=fresh,
    )
    await db.commit()
    new = (await read(client, setup)).json()
    assert new["certificateId"] != old["certificateId"]
    assert len(new["courses"]) == 1 and len(old["courses"]) == 2
    assert new["assessment"]["amountAssessed"] == "385.75"
    for dto, expected in ((old, "SUPERSEDED"), (new, "CURRENT")):
        response = await client.get(VERIFY + dto["verificationUrl"].split("/")[-1])
        assert response.json()["status"] == expected
        assert response.json()["issuedAt"] == dto["issuedAt"]


async def test_ac_enrl_003_9_atomic_issue_status_one_audit_and_retry(client, setup, db):
    first = await issue(client, setup)
    before = (await read(client, setup)).json()
    second = await issue(client, setup)
    assert first.json() == second.json()
    assert (await read(client, setup)).json() == before
    enrollment = await db.get(TermEnrollment, setup.enrollment_id)
    assert enrollment.enrollment_status == "OFFICIALLY_ENROLLED"
    rows = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == "enrollment.cor_issued")
        )
    )
    assert len(rows) == 1
    assert rows[0].prior_state["status"] == "ENROLLED_PENDING"
    assert rows[0].new_state["status"] == "OFFICIALLY_ENROLLED"
    assert rows[0].actor_id == setup.student_id
    from cbpupsis_shared.domains.enrollment.service import enrolled_sections

    assert len(await enrolled_sections(db, setup.student_id, setup.term_id)) == 2


@pytest.mark.parametrize("failure,status", [(RuntimeError, 500), (TimeoutError, 503)])
async def test_generation_failure_no_partial_attachment(
    client, setup, monkeypatch, caplog, failure, status
):
    from httpx import ASGITransport, AsyncClient

    await issue(client, setup)

    def fail(_):
        raise failure("sensitive renderer data must not leak")

    monkeypatch.setattr(cor_exports, "render_pdf", fail)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as handler_client:
        response = await read(handler_client, setup, "pdf")
    assert response.status_code == status
    if status == 503:
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    else:
        assert response.json()["detail"] == "Internal server error"
        [record] = [r for r in caplog.records if r.name == "cbpupsis_core.exceptions"]
        assert record.error_type == "RuntimeError"
        assert record.exc_info is None
    assert "no-store" in response.headers["cache-control"]
    assert "content-disposition" not in response.headers
    assert b"sensitive" not in response.content
    assert "sensitive renderer" not in caplog.text


async def test_invalid_renderer_output_returns_503(client, setup, monkeypatch):
    await issue(client, setup)
    monkeypatch.setattr(cor_exports, "render_pdf", lambda _: b"not-a-pdf")
    response = await read(client, setup, "pdf")
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "content-disposition" not in response.headers


async def test_unissued_get_without_academic_service_is_read_only_409(
    client, setup, db, monkeypatch
):
    monkeypatch.setattr(academic_verdict, "certificate_eligible", REAL_VERDICT)
    response = await read(client, setup)
    assert response.status_code == 409
    assert response.json()["code"] == "COR_ENROLLMENT_NOT_FINAL"
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 0
    assert (
        await db.get(TermEnrollment, setup.enrollment_id)
    ).enrollment_status == "ENROLLED_PENDING"


async def test_reissue_failure_caller_rolls_back_every_change(
    client, setup, db, monkeypatch
):
    await issue(client, setup)
    old = (await read(client, setup)).json()
    student_id, term_id, actor_id = setup.student_id, setup.term_id, setup.faculty_id
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    fresh = await billing.stage_assessment(
        db,
        student_id=student_id,
        term_id=term_id,
        courses=[
            billing.AssessmentCourse(section_id=setup.section_ids[1], units=Decimal(3))
        ],
        due_date=date(2026, 12, 1),
    )

    def fail(*args, **kwargs):
        raise CertificateUnavailable

    monkeypatch.setattr(certificate.audit, "stage", fail)
    with pytest.raises(CertificateUnavailable):
        await certificate.stage_reissue(
            db,
            student_id=student_id,
            term_id=term_id,
            actor_id=actor_id,
            assessment=fresh,
        )
    await db.rollback()  # The approval owner, not the hook, controls rollback.
    assert (await read(client, setup)).json() == old
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 1
    assert (
        await billing.read_stored_assessment(db, student_id, term_id)
    ).assessment.amountAssessed == "686.50"


async def test_meeting_overflow_fails_closed(client, setup, monkeypatch):
    original = certificate.sections.schedule_sections

    async def overflow(*args):
        rows = await original(*args)
        rows[0].meetings = [rows[0].meetings[0]] * 1001
        return rows

    monkeypatch.setattr(certificate.sections, "schedule_sections", overflow)
    response = await client.post(
        URL, json={"academicTermId": str(setup.term_id)}, headers=setup.headers
    )
    assert response.status_code == 503


async def test_unexpected_errors_log_only_safe_metadata(
    client, setup, monkeypatch, caplog
):
    from httpx import ASGITransport, AsyncClient

    async def fail(*args):
        raise RuntimeError("private token and monetary record must not be logged")

    monkeypatch.setattr(certificate.repository, "by_hash", fail)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        response = await safe_client.get(VERIFY + secrets.token_urlsafe(32))
    assert response.status_code == 500
    assert "no-store" in response.headers["cache-control"]
    records = [r for r in caplog.records if r.name == "cbpupsis_core.exceptions"]
    assert records and all(r.exc_info is None for r in records)
    assert all("monetary" not in r.getMessage() for r in records)


async def test_representative_ten_course_pdf(client, setup):
    from cbpupsis_shared.domains.enrollment.certificate_schemas import CertificateRead

    await issue(client, setup)
    dto = CertificateRead.model_validate((await read(client, setup)).json())
    courses = [
        dto.courses[index % 2].model_copy(
            update={
                "sectionId": uuid.uuid4(),
                "code": f"CS{index + 1:03}",
                "section": f"BSCS 1-{index + 1}",
            },
            deep=True,
        )
        for index in range(10)
    ]
    inputs = setup.assessment.inputs.model_copy(
        update={
            "courses": [
                billing.AssessmentCourse(section_id=c.sectionId, units=c.units)
                for c in courses
            ]
        }
    )
    dto = dto.model_copy(
        update={
            "courses": courses,
            "totalUnits": Decimal(30),
            "assessment": billing.calculate_assessment(inputs),
        }
    )
    content = cor_exports.render_pdf(dto)
    assert len(PdfReader(BytesIO(content)).pages) == 1
    output = os.environ.get("COR_LAYOUT_OUTPUT")
    if output:
        Path(output).write_bytes(content)  # Synthetic generated artifact, not source.


async def test_unknown_malformed_and_fixed_ip_rate_limit(client, setup, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    try:
        for index in range(10):
            response = await client.get(
                VERIFY + ("malformed" if index == 0 else secrets.token_urlsafe(32)),
                headers={"X-Forwarded-For": "192.0.2.10"},
            )
            assert (
                response.status_code == 404
                and response.json()["code"] == "RESOURCE_NOT_FOUND"
            )
        response = await client.get(
            VERIFY + secrets.token_urlsafe(32),
            headers={"X-Forwarded-For": "192.0.2.10"},
        )
        assert response.status_code == 429 and response.json()["code"] == "rate_limited"
        assert (
            await client.get(VERIFY + "bad", headers={"X-Forwarded-For": "192.0.2.11"})
        ).status_code == 404
    finally:
        limiter.reset()


@pytest.mark.parametrize("failure", ["audit", "encryption", "academic", "commit"])
async def test_issue_failure_rolls_back_all_business_effects(
    client, setup, db, monkeypatch, failure
):
    flushed_failure = []
    if failure == "audit":

        def fail(*args, **kwargs):
            raise CertificateUnavailable

        monkeypatch.setattr(certificate.audit, "stage", fail)
    elif failure == "encryption":
        monkeypatch.setattr(settings, "cor_encryption_key", None)
    elif failure == "commit":
        original_commit = db.commit

        async def fail_after_business_flush():
            await db.flush()
            if await db.scalar(select(CorIssuance.id).limit(1)):
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(AuditEntry)
                        .where(AuditEntry.action == "enrollment.cor_issued")
                    )
                    == 1
                )
                flushed_failure.append(True)
                raise CertificateUnavailable
            await original_commit()

        monkeypatch.setattr(db, "commit", fail_after_business_flush)
    else:
        monkeypatch.setattr(academic_verdict, "certificate_eligible", REAL_VERDICT)
    response = await client.post(
        URL, json={"academicTermId": str(setup.term_id)}, headers=setup.headers
    )
    assert response.status_code == 503
    if failure == "commit":
        assert flushed_failure == [True]
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 0
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.action == "enrollment.cor_issued")
        )
        == 0
    )
    assert (
        await db.get(TermEnrollment, setup.enrollment_id)
    ).enrollment_status == "ENROLLED_PENDING"


@pytest.mark.parametrize(
    "qualified,forfeited,net",
    [(True, False, "0.00"), (False, False, "85.01"), (True, True, "85.01")],
)
def test_decimal_rounding_and_fhe_inputs(qualified, forfeited, net):
    inputs = AssessmentInputs(
        student_id=uuid.uuid4(),
        term_id=uuid.uuid4(),
        program_id=uuid.uuid4(),
        fee_structure_id=uuid.uuid4(),
        courses=[billing.AssessmentCourse(section_id=uuid.uuid4(), units=Decimal(3))],
        tuition_per_unit=Decimal("0.335"),
        registration_fee=Decimal(50),
        lab_fee=Decimal(24),
        misc_fee=Decimal(10),
        fhe_qualified=qualified,
        fhe_forfeited=forfeited,
        fhe_forfeiture_reason="Stored decision" if forfeited else None,
    )
    result = billing.calculate_assessment(inputs)
    assert result.lines[0].amount == "1.01" and result.netPayable == net
