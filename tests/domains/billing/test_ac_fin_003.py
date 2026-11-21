"""AC-FIN-003: Save and validate the fee structure for a term (#108 / #86).

Covers AC-FIN-003.1 through AC-FIN-003.9:
- PUT with valid values returns 200, GET returns the same values (AC-FIN-003.1).
- Negative rate or fee returns 422 VALIDATION_ERROR naming field (AC-FIN-003.2).
- Installment sums != 100 return 422 INSTALLMENT_SUM_INVALID (AC-FIN-003.3).
- Installments of 40, 30, 30 return 200 (AC-FIN-003.4).
- Due dates out of order return 422 INSTALLMENT_DATE_ORDER (AC-FIN-003.5).
- Covered subsidy fees: tuition and lab marked covered (AC-FIN-003.6).
- After one assessment uses structure, returns 409 FEE_STRUCTURE_IN_USE (AC-FIN-003.7).
- Scope test per role: Finance 200; Registrar, Dean, Chair 403 (AC-FIN-003.8).
- Each save writes one immutable audit row (AC-FIN-003.9).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import AdminProfile, StudentProfile, User
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.auth.service.tokens import issue_role_session
from cbpupsis_shared.domains.billing import assessment as billing_assessment
from cbpupsis_shared.domains.billing.schemas import AssessmentCourse
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from scripts import seed_iam

PASSWORD = "Test-Password-Finance-123!"


async def _make_admin(
    db: AsyncSession,
    *,
    email: str,
    position: str,
) -> tuple[User, dict[str, str]]:
    await seed_iam.seed(db)
    user = User(
        email=email,
        full_name=f"Admin {position.title()}",
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        AdminProfile(
            user_id=user.id,
            position=position,
            is_active=True,
        )
    )
    admin_group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    assert admin_group is not None
    await iam_service.add_user_to_group(db, user.id, admin_group.id)
    await iam_service.stage_admin_position_policies(db, [(user.id, position)])
    await db.commit()

    pair = await issue_role_session(
        db,
        user.id,
        role=ROLE_ADMIN,
        ip_address="127.0.0.1",
        mfa=True,
        claims={"position": position},
    )
    return user, {"Authorization": f"Bearer {pair.access_token}"}


async def _make_term(db: AsyncSession) -> AcademicTerm:
    term = AcademicTerm(
        academic_year=f"2026-2027-{uuid.uuid4().hex[:6]}",
        semester="FIRST_SEMESTER",
        term_type="REGULAR",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 15),
        is_current=True,
        regular_enrollment_start=datetime.now(UTC),
        regular_enrollment_end=datetime.now(UTC),
        add_drop_deadline=datetime.now(UTC),
        grade_submission_deadline=datetime.now(UTC),
    )
    db.add(term)
    await db.commit()
    return term


async def _make_program(db: AsyncSession, code: str = "BSCS") -> Program:
    prog = Program(
        code=code,
        name=f"Bachelor of Science in {code}",
        department="Department of Computer and Information Sciences",
        college="College of Computer and Information Sciences",
        total_units_required=Decimal("140.0"),
        is_active=True,
    )
    db.add(prog)
    await db.commit()
    return prog


async def _make_student(
    db: AsyncSession,
    program_id: uuid.UUID,
    *,
    fhe_qualified: bool = True,
    fhe_forfeited: bool = False,
) -> StudentProfile:
    user = User(
        email=f"student.{uuid.uuid4().hex[:6]}@example.com",
        full_name="Test Student",
        password_hash=hash_password("Password123!"),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    profile = StudentProfile(
        user_id=user.id,
        student_number=f"2026-{uuid.uuid4().int % 100000:05d}-MN-0",
        program_id=program_id,
        year_level=1,
        fhe_qualified=fhe_qualified,
        fhe_forfeited=fhe_forfeited,
    )
    db.add(profile)
    await db.commit()
    return profile


@pytest.mark.asyncio
async def test_ac_fin_003_1_save_and_read(
    client: AsyncClient, db: AsyncSession
) -> None:
    """PUT with valid values returns 200, GET returns same values."""
    _, headers = await _make_admin(db, email="fin1@example.com", position="finance")
    term = await _make_term(db)
    prog = await _make_program(db, code="BSCS1")

    payload = {
        "tuitionPerUnit": "550.00",
        "programTuitionRates": [
            {"programId": str(prog.id), "tuitionPerUnit": "600.00"}
        ],
        "registrationFee": "1250.00",
        "labFee": "3500.00",
        "miscFee": "800.00",
        "lateFeeRate": "3.50",
        "dueDate": "2026-08-15",
        "subsidyCoveredFees": ["TUITION", "LAB"],
        "installments": [
            {"installmentNumber": 1, "percentage": "40.00", "dueDate": "2026-09-15"},
            {"installmentNumber": 2, "percentage": "30.00", "dueDate": "2026-10-15"},
            {"installmentNumber": 3, "percentage": "30.00", "dueDate": "2026-11-15"},
        ],
    }

    url = f"/api/v1/admin/fee-structures/{term.id}"
    put_res = await client.put(url, json=payload, headers=headers)
    assert put_res.status_code == 200, put_res.text
    saved = put_res.json()

    assert saved["academicTermId"] == str(term.id)
    assert saved["tuitionPerUnit"] == "550.00"
    assert saved["registrationFee"] == "1250.00"
    assert saved["labFee"] == "3500.00"
    assert saved["miscFee"] == "800.00"
    assert saved["lateFeeRate"] == "3.50"
    assert saved["dueDate"] == "2026-08-15"
    assert saved["subsidyCoveredFees"] == ["TUITION", "LAB"]
    assert len(saved["programTuitionRates"]) == 1
    assert saved["programTuitionRates"][0]["programId"] == str(prog.id)
    assert saved["programTuitionRates"][0]["tuitionPerUnit"] == "600.00"
    assert len(saved["installments"]) == 3

    # GET returns identical values
    get_res = await client.get(url, headers=headers)
    assert get_res.status_code == 200
    assert get_res.json() == saved

    # An assessment for that term uses the saved fee structure
    student = await _make_student(db, prog.id)
    stored = await billing_assessment.stage_assessment(
        db,
        student_id=student.user_id,
        term_id=term.id,
        courses=[AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.0"))],
        due_date=date(2026, 8, 15),
    )
    # 3 units * 600.00 = 1800.00 tuition
    # 1250 reg + 3500 lab + 800 misc = 5550 fixed
    # total assessed = 7350.00
    # covered: tuition (1800) + lab (3500) = 5300.00
    assert stored.assessment.amountAssessed == "7350.00"
    assert stored.assessment.subsidyApplied == "5300.00"
    assert stored.assessment.netPayable == "2050.00"


@pytest.mark.asyncio
async def test_ac_fin_003_2_negative_rate_or_fee(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A negative rate or fee returns 422 VALIDATION_ERROR naming the field."""
    _, headers = await _make_admin(db, email="fin2@example.com", position="finance")
    term = await _make_term(db)

    # 1. Negative tuition_per_unit
    payload = {
        "tuitionPerUnit": "-10.00",
        "registrationFee": "100.00",
        "labFee": "50.00",
        "miscFee": "50.00",
        "lateFeeRate": "0.00",
    }
    url = f"/api/v1/admin/fee-structures/{term.id}"
    res = await client.put(url, json=payload, headers=headers)
    assert res.status_code == 422
    data = res.json()
    assert data["code"] == "VALIDATION_ERROR"
    assert "tuition_per_unit" in data.get("field", "") or "tuition_per_unit" in str(
        data
    )

    # 2. Negative lab fee
    payload["tuitionPerUnit"] = "100.00"
    payload["labFee"] = "-5.00"
    res2 = await client.put(url, json=payload, headers=headers)
    assert res2.status_code == 422
    assert res2.json()["code"] == "VALIDATION_ERROR"
    assert "lab_fee" in res2.json().get("field", "") or "lab_fee" in str(res2.json())

    # Verify nothing was saved
    get_res = await client.get(url, headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["tuitionPerUnit"] == "0.00"


@pytest.mark.asyncio
async def test_ac_fin_003_3_installment_sum_invalid(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Installments not summing to 100 return 422 INSTALLMENT_SUM_INVALID."""
    _, headers = await _make_admin(db, email="fin3@example.com", position="finance")
    term = await _make_term(db)
    url = f"/api/v1/admin/fee-structures/{term.id}"

    # 30, 30, 39.5 = 99.5
    res1 = await client.put(
        url,
        json={
            "tuitionPerUnit": "100.00",
            "installments": [
                {
                    "installmentNumber": 1,
                    "percentage": "30.00",
                    "dueDate": "2026-09-01",
                },
                {
                    "installmentNumber": 2,
                    "percentage": "30.00",
                    "dueDate": "2026-10-01",
                },
                {
                    "installmentNumber": 3,
                    "percentage": "39.50",
                    "dueDate": "2026-11-01",
                },
            ],
        },
        headers=headers,
    )
    assert res1.status_code == 422
    assert res1.json()["code"] == "INSTALLMENT_SUM_INVALID"

    # 30, 30, 40.5 = 100.5
    res2 = await client.put(
        url,
        json={
            "tuitionPerUnit": "100.00",
            "installments": [
                {
                    "installmentNumber": 1,
                    "percentage": "30.00",
                    "dueDate": "2026-09-01",
                },
                {
                    "installmentNumber": 2,
                    "percentage": "30.00",
                    "dueDate": "2026-10-01",
                },
                {
                    "installmentNumber": 3,
                    "percentage": "40.50",
                    "dueDate": "2026-11-01",
                },
            ],
        },
        headers=headers,
    )
    assert res2.status_code == 422
    assert res2.json()["code"] == "INSTALLMENT_SUM_INVALID"


@pytest.mark.asyncio
async def test_ac_fin_003_4_installment_sum_valid(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Installments of 40, 30, 30 return 200."""
    _, headers = await _make_admin(db, email="fin4@example.com", position="finance")
    term = await _make_term(db)
    url = f"/api/v1/admin/fee-structures/{term.id}"

    res = await client.put(
        url,
        json={
            "tuitionPerUnit": "100.00",
            "installments": [
                {
                    "installmentNumber": 1,
                    "percentage": "40.00",
                    "dueDate": "2026-09-01",
                },
                {
                    "installmentNumber": 2,
                    "percentage": "30.00",
                    "dueDate": "2026-10-01",
                },
                {
                    "installmentNumber": 3,
                    "percentage": "30.00",
                    "dueDate": "2026-11-01",
                },
            ],
        },
        headers=headers,
    )
    assert res.status_code == 200
    assert len(res.json()["installments"]) == 3


@pytest.mark.asyncio
async def test_ac_fin_003_5_installment_date_order(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Out-of-order due dates return 422 INSTALLMENT_DATE_ORDER naming both."""
    _, headers = await _make_admin(db, email="fin5@example.com", position="finance")
    term = await _make_term(db)
    url = f"/api/v1/admin/fee-structures/{term.id}"

    res = await client.put(
        url,
        json={
            "tuitionPerUnit": "100.00",
            "installments": [
                {
                    "installmentNumber": 1,
                    "percentage": "50.00",
                    "dueDate": "2026-03-01",
                },
                {
                    "installmentNumber": 2,
                    "percentage": "50.00",
                    "dueDate": "2026-02-01",
                },
            ],
        },
        headers=headers,
    )
    assert res.status_code == 422
    data = res.json()
    assert data["code"] == "INSTALLMENT_DATE_ORDER"
    text_data = str(data)
    assert "2026-03-01" in text_data
    assert "2026-02-01" in text_data


@pytest.mark.asyncio
async def test_ac_fin_003_6_subsidy_covered_fees(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Subsidy covers configured fee lines; non-covered remain in netPayable."""
    _, headers = await _make_admin(db, email="fin6@example.com", position="finance")
    term = await _make_term(db)
    prog = await _make_program(db, code="BSCS6")

    # Set up fee structure covering tuition and laboratory only
    url = f"/api/v1/admin/fee-structures/{term.id}"
    put_res = await client.put(
        url,
        json={
            "tuitionPerUnit": "200.00",
            "registrationFee": "100.00",
            "labFee": "300.00",
            "miscFee": "50.00",
            "subsidyCoveredFees": ["TUITION", "LAB"],
        },
        headers=headers,
    )
    assert put_res.status_code == 200

    # Student with 3 units
    student = await _make_student(db, prog.id, fhe_qualified=True)
    stored = await billing_assessment.stage_assessment(
        db,
        student_id=student.user_id,
        term_id=term.id,
        courses=[AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.0"))],
        due_date=date(2026, 8, 15),
    )
    # Tuition: 3 * 200 = 600.00
    # Reg: 100.00 (not covered)
    # Lab: 300.00 (covered)
    # Misc: 50.00 (not covered)
    # Total: 1050.00
    # Subsidy: 600 + 300 = 900.00
    # Net payable: 1050 - 900 = 150.00
    assert stored.assessment.amountAssessed == "1050.00"
    assert stored.assessment.subsidyApplied == "900.00"
    assert stored.assessment.netPayable == "150.00"


@pytest.mark.asyncio
async def test_ac_fin_003_7_fee_structure_in_use(
    client: AsyncClient, db: AsyncSession
) -> None:
    """In-use fee structure returns 409 FEE_STRUCTURE_IN_USE upon mutation."""
    _, headers = await _make_admin(db, email="fin7@example.com", position="finance")
    term = await _make_term(db)
    prog = await _make_program(db, code="BSCS7")
    url = f"/api/v1/admin/fee-structures/{term.id}"

    # Save initial fee structure
    res = await client.put(
        url,
        json={"tuitionPerUnit": "150.00", "registrationFee": "50.00"},
        headers=headers,
    )
    assert res.status_code == 200

    # Create an assessment using it
    student = await _make_student(db, prog.id)
    initial_assessment = await billing_assessment.stage_assessment(
        db,
        student_id=student.user_id,
        term_id=term.id,
        courses=[AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.0"))],
        due_date=date(2026, 8, 15),
    )
    assert initial_assessment.assessment.amountAssessed == "500.00"

    # Attempt to modify the fee structure -> 409 FEE_STRUCTURE_IN_USE
    refusal = await client.put(
        url,
        json={"tuitionPerUnit": "300.00", "registrationFee": "100.00"},
        headers=headers,
    )
    assert refusal.status_code == 409
    assert refusal.json()["code"] == "FEE_STRUCTURE_IN_USE"

    # Verify existing stored assessment is completely unchanged
    stored_again = await billing_assessment.read_stored_assessment(
        db, student.user_id, term.id
    )
    assert stored_again is not None
    assert stored_again.assessment.amountAssessed == "500.00"


@pytest.mark.asyncio
async def test_ac_fin_003_8_rbac_scope(client: AsyncClient, db: AsyncSession) -> None:
    """Finance gets 200 on GET and PUT. Registrar, Dean, Chair sessions get 403."""
    _, fin_headers = await _make_admin(db, email="fin8@example.com", position="finance")
    _, reg_headers = await _make_admin(
        db, email="reg8@example.com", position="registrar"
    )
    _, dean_headers = await _make_admin(db, email="dean8@example.com", position="dean")
    _, chair_headers = await _make_admin(
        db, email="chair8@example.com", position="chairperson"
    )

    term = await _make_term(db)
    url = f"/api/v1/admin/fee-structures/{term.id}"

    # Finance gets 200 on PUT and GET
    put_fin = await client.put(
        url, json={"tuitionPerUnit": "250.00"}, headers=fin_headers
    )
    assert put_fin.status_code == 200

    get_fin = await client.get(url, headers=fin_headers)
    assert get_fin.status_code == 200

    # Registrar, Dean, Chair get 403 on both PUT and GET
    for non_fin_headers in (reg_headers, dean_headers, chair_headers):
        get_res = await client.get(url, headers=non_fin_headers)
        assert get_res.status_code == 403

        put_res = await client.put(
            url, json={"tuitionPerUnit": "999.00"}, headers=non_fin_headers
        )
        assert put_res.status_code == 403


@pytest.mark.asyncio
async def test_ac_fin_003_9_audit_trail_and_immutability(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Save writes immutable audit row with actor, role, values, and UTC+8 time."""
    user, headers = await _make_admin(db, email="fin9@example.com", position="finance")
    term = await _make_term(db)
    url = f"/api/v1/admin/fee-structures/{term.id}"

    res = await client.put(
        url,
        json={"tuitionPerUnit": "350.00", "registrationFee": "200.00"},
        headers=headers,
    )
    assert res.status_code == 200

    # Verify audit row was created
    term_id_str = str(term.id)
    audit = await db.scalar(
        select(AuditEntry).where(
            AuditEntry.action == "billing.fee_structure.saved",
            AuditEntry.target_id == term_id_str,
        )
    )
    assert audit is not None
    audit_id = audit.id
    assert audit.actor_id == user.id
    assert audit.payload.get("role") == "finance"
    assert audit.payload.get("timezone") == "UTC+8"
    assert audit.new_state is not None
    assert audit.occurred_at is not None

    # Test that UPDATE and DELETE on audit row fail (append-only trigger / listener)
    with pytest.raises(PermissionError, match="audit entries are append-only"):
        audit.action = "tampered"
        await db.flush()

    await db.rollback()

    audit = await db.get(AuditEntry, audit_id)
    assert audit is not None

    with pytest.raises(PermissionError, match="audit entries are append-only"):
        await db.delete(audit)
        await db.flush()
