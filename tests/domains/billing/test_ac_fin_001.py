"""Issue #106 acceptance checks over real stored assessments and HTTP sessions."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.auth import UserActiveSession
from cbpupsis_database.models.billing import (
    FeeStructure,
    Payment,
    StudentBillingAccount,
)
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.notifications import Notification
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.auth.service.tokens import issue_role_session
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.billing.exceptions import BillingAccountUnavailableError
from cbpupsis_shared.domains.iam import service as iam
from scripts import seed_iam

ACCOUNT = "/api/v1/student/account"
PAYMENTS = "/api/v1/student/payments"


async def _overdue_messages(db):
    return list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == "billing.account_overdue"
            )
        )
    )


@pytest.fixture
async def account(db):
    await seed_iam.seed(db)
    program = Program(
        code="FIN106",
        name="Synthetic Billing Program",
        department="Computer Science",
        total_units_required=Decimal("140"),
    )
    student = User(
        email="finance106@example.com",
        password_hash=hash_password("Synthetic-Password-106!"),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    term = AcademicTerm(
        academic_year="2026-2027",
        semester="First Semester",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 1, 1),
        is_current=True,
        regular_enrollment_start=datetime.now(UTC) - timedelta(days=1),
        regular_enrollment_end=datetime.now(UTC) + timedelta(days=1),
        add_drop_deadline=datetime.now(UTC) + timedelta(days=7),
        grade_submission_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    db.add_all([program, student, term])
    await db.flush()
    profile = StudentProfile(
        user_id=student.id,
        student_number="2026-00106-MN-0",
        program_id=program.id,
        year_level=1,
        enrollment_status="PENDING",
    )
    fee = FeeStructure(
        program_id=program.id,
        academic_term_id=term.id,
        tuition_per_unit=Decimal("100.11"),
        registration_fee=Decimal("50.01"),
        lab_fee=Decimal("20.02"),
        misc_fee=Decimal("10.03"),
        subsidy_covered_fees=["TUITION", "REGISTRATION", "LAB", "MISC"],
    )
    db.add_all([profile, fee])
    await db.flush()
    group = await db.scalar(select(Group).where(Group.name == "Students"))
    await iam.add_user_to_group(db, student.id, group.id)
    courses = [billing.AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.5"))]

    async def stage():
        stored = await billing.stage_assessment(
            db,
            student_id=student.id,
            term_id=term.id,
            courses=courses,
            due_date=date(2099, 1, 1),
        )
        await db.commit()
        return stored

    await stage()
    token = await issue_role_session(
        db, student.id, role="student", ip_address="127.0.0.1"
    )
    row = await db.scalar(select(StudentBillingAccount))
    return {
        "student": student,
        "profile": profile,
        "fee": fee,
        "term": term,
        "row": row,
        "stage": stage,
        "headers": {"Authorization": f"Bearer {token.access_token}"},
    }


async def test_ac_fin_001_1_qualified_account_zero_payable(client, account):
    response = await client.get(ACCOUNT, headers=account["headers"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["amountAssessed"] == data["subsidyApplied"] == "430.45"
    assert data["netPayable"] == "0.00"
    assert data["paymentStatus"] == "PAID"
    assert data["holds"] == []
    assert "no-store" in response.headers["cache-control"]


async def test_ac_fin_001_2_only_covered_lines_are_subsidized(client, db, account):
    account["fee"].subsidy_covered_fees = ["tuition_fee", "laboratory"]
    await account["stage"]()
    response = await client.get(ACCOUNT, headers=account["headers"])
    assert response.status_code == 200, response.text
    data = response.json()
    covered = [line for line in data["lines"] if line["coveredByFhe"]]
    assert {line["description"] for line in covered} == {"Tuition", "Laboratory fee"}
    assert sum((Decimal(line["amount"]) for line in covered), Decimal(0)) == Decimal(
        data["subsidyApplied"]
    )
    assert data["subsidyApplied"] == "370.41"
    assert data["netPayable"] == "60.04"
    assert data["holds"][0]["amount_outstanding"] == "60.04"


async def test_ac_fin_001_6_non_qualified_pays_full_assessment(client, account):
    account["profile"].fhe_qualified = False
    await account["stage"]()
    response = await client.get(ACCOUNT, headers=account["headers"])
    data = response.json()
    assert response.status_code == 200, response.text
    assert data["subsidyApplied"] == "0.00"
    assert data["netPayable"] == data["amountAssessed"] == "430.45"
    assert data["paymentStatus"] == "DUE"
    assert not any(line["coveredByFhe"] for line in data["lines"])


@pytest.mark.parametrize(
    "paid,expected",
    [("0.00", "DUE"), ("1.01", "PARTIAL"), ("430.45", "PAID"), ("500.00", "PAID")],
)
async def test_ac_fin_001_1_payment_status_uses_balance(
    client, account, paid, expected
):
    account["profile"].fhe_qualified = False
    await account["stage"]()
    account["row"].amount_paid = Decimal(paid)
    await account["stage"]()
    data = (await client.get(ACCOUNT, headers=account["headers"])).json()
    assert data["paymentStatus"] == expected
    assert Decimal(data["netPayable"]) == max(
        Decimal("430.45") - Decimal(paid), Decimal(0)
    )


async def test_ac_fin_001_8_payment_history_ordering_and_ownership(client, db, account):
    now = datetime.now(UTC)
    rows = [
        (uuid.UUID(int=1), now, now - timedelta(days=2), "first"),
        (uuid.UUID(int=2), now, now - timedelta(days=1), "second"),
        (uuid.UUID(int=3), now, now - timedelta(days=1), "third"),
        (uuid.UUID(int=4), now - timedelta(days=5), None, "pending"),
    ]
    for ident, created, paid, reference in rows:
        db.add(
            Payment(
                id=ident,
                billing_account_id=account["row"].id,
                student_id=account["student"].id,
                idempotency_key=f"history-{ident}",
                merchant_reference=f"history-{ident}",
                amount=Decimal("0.01"),
                payment_method="SYNTHETIC",
                payment_status="PENDING" if paid is None else "PAID",
                gateway_reference=reference,
                created_at=created,
                paid_at=paid,
            )
        )
    other = User(
        email="foreign106@example.com",
        password_hash=hash_password("Synthetic-Other-106!"),
        is_active=True,
    )
    db.add(other)
    await db.flush()
    foreign_account = StudentBillingAccount(
        student_id=other.id,
        academic_term_id=account["term"].id,
        gross_amount=Decimal("100"),
        due_date=date(2099, 1, 1),
    )
    db.add(foreign_account)
    await db.flush()
    db.add(
        Payment(
            billing_account_id=foreign_account.id,
            student_id=other.id,
            idempotency_key="foreign-history",
            merchant_reference="foreign-history",
            amount=Decimal("100"),
            payment_method="SYNTHETIC",
            payment_status="PAID",
            gateway_reference="foreign",
            paid_at=now + timedelta(days=1),
            created_at=now,
        )
    )
    await db.commit()
    response = await client.get(PAYMENTS, headers=account["headers"])
    assert response.status_code == 200, response.text
    assert [p["reference"] for p in response.json()] == [
        "third",
        "second",
        "first",
        "pending",
    ]
    assert all(p["amount"] == "0.01" for p in response.json())
    assert all(
        set(p) == {"date", "amount", "reference", "status"} for p in response.json()
    )
    assert "no-store" in response.headers["cache-control"]


async def test_ac_fin_001_8_empty_payment_history(client, account):
    response = await client.get(PAYMENTS, headers=account["headers"])
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("role", ["faculty", "admin", "superadmin", None])
@pytest.mark.parametrize("url", [ACCOUNT, PAYMENTS])
async def test_ac_fin_001_1_non_student_session_is_hidden(
    client, db, account, role, url
):
    if role == "superadmin":
        group = await db.scalar(select(Group).where(Group.name == "Superadmins"))
        await iam.add_user_to_group(db, account["student"].id, group.id)
    token = await issue_role_session(
        db, account["student"].id, role=role, ip_address="127.0.0.1", mfa=True
    )
    response = await client.get(
        url, headers={"Authorization": f"Bearer {token.access_token}"}
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("url", [ACCOUNT, PAYMENTS])
async def test_ac_fin_001_1_expired_session(client, db, account, url):
    sessions = (await db.scalars(select(UserActiveSession))).all()
    for session in sessions:
        session.last_activity_at = datetime.now(UTC) - timedelta(days=2)
    await db.commit()
    response = await client.get(url, headers=account["headers"])
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "AUTH_SESSION_EXPIRED"


async def test_ac_fin_001_10_money_is_decimal_strings_and_lines_sum(client, account):
    data = (await client.get(ACCOUNT, headers=account["headers"])).json()
    for name in ("amountAssessed", "subsidyApplied", "netPayable"):
        assert isinstance(data[name], str)
    assert all(isinstance(line["amount"], str) for line in data["lines"])
    assert sum(
        (Decimal(line["amount"]) for line in data["lines"]), Decimal(0)
    ) == Decimal(data["amountAssessed"])


async def test_ac_fin_001_3_stale_flag_cannot_hold_zero_account(db, account):
    holds = await billing.get_student_holds(
        db, student_id=account["student"].id, has_financial_hold=True
    )
    assert not any(hold.type == "FINANCIAL" for hold in holds)


async def test_ac_fin_001_3_stale_paid_status_cannot_hide_positive_balance(db, account):
    account["profile"].fhe_qualified = False
    await account["stage"]()
    account["row"].payment_status = "paid"
    await db.commit()
    holds = await billing.get_student_holds(db, student_id=account["student"].id)
    assert holds[0].amount_outstanding == "430.45"


async def test_ac_fin_001_9_subsidy_transitions_are_audited_once(db, account):
    await account["stage"]()
    account["profile"].fhe_forfeited = True
    account["profile"].fhe_forfeiture_reason = "Synthetic approved forfeiture decision"
    await account["stage"]()
    await account["stage"]()
    rows = (
        await db.scalars(
            select(AuditEntry)
            .where(AuditEntry.action.like("billing.subsidy_%"))
            .order_by(AuditEntry.occurred_at)
        )
    ).all()
    assert [row.action for row in rows] == [
        "billing.subsidy_applied",
        "billing.subsidy_withdrawn",
    ]
    assert rows[0].prior_state["subsidyApplied"] == "0.00"
    assert rows[0].new_state["subsidyApplied"] == "430.45"
    assert rows[1].prior_state["subsidyApplied"] == "430.45"
    assert rows[1].new_state["subsidyApplied"] == "0.00"
    assert (
        rows[1].new_state["fheForfeitureReason"]
        == account["profile"].fhe_forfeiture_reason
    )
    for row in rows:
        assert row.payload["rule"]
        assert datetime.fromisoformat(row.payload["timeUtc8"]).utcoffset() == timedelta(
            hours=8
        )


async def test_ac_fin_001_9_subsidy_and_audit_roll_back_together(db, account):
    student_id = account["student"].id
    term_id = account["term"].id
    account["profile"].fhe_qualified = False
    await billing.stage_assessment(
        db,
        student_id=student_id,
        term_id=term_id,
        courses=[
            billing.AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.5"))
        ],
        due_date=date(2099, 1, 1),
    )
    await db.flush()
    await db.rollback()
    stored = await billing.read_stored_assessment(db, student_id, term_id)
    assert stored.assessment.subsidyApplied == "430.45"
    rows = (
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action.like("billing.subsidy_%"))
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].action == "billing.subsidy_applied"


async def test_ac_fin_001_4_stored_forfeiture_is_visible_without_invented_residency(
    client, db, account
):
    account["profile"].fhe_forfeited = True
    account["profile"].fhe_forfeiture_reason = "Approved academic-owner decision"
    await db.commit()
    for _ in range(2):
        response = await client.get(ACCOUNT, headers=account["headers"])
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["fheForfeited"] is True
        assert data["fheForfeitureReason"] == "Approved academic-owner decision"
        assert data["subsidyApplied"] == "0.00"
        assert data["netPayable"] == data["amountAssessed"]
    rows = (
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == "billing.subsidy_withdrawn")
        )
    ).all()
    assert len(rows) == 1


async def test_ac_fin_001_7_due_date_boundary_and_one_flat_fine(db, account):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2026, 11, 16)
    await db.commit()
    before = await billing.read_account(
        db, student_id, as_of=datetime(2026, 11, 16, 15, 59, 59, tzinfo=UTC)
    )
    assert before.paymentStatus == "DUE"
    assert not await _overdue_messages(db)
    for _ in range(3):
        after = await billing.read_account(
            db, student_id, as_of=datetime(2026, 11, 16, 16, tzinfo=UTC)
        )
        assert after.paymentStatus == "OVERDUE"
        assert after.amountAssessed == after.netPayable == "530.45"
        fine = [line for line in after.lines if line.description == "Late payment fine"]
        assert len(fine) == 1
        assert fine[0].amount == "100.00" and fine[0].coveredByFhe is False
        assert after.holds[0].amount_outstanding == "530.45"
    messages = await _overdue_messages(db)
    assert len(messages) == 1
    assert messages[0].event_name == "billing.account_overdue"
    assert messages[0].payload["user_id"] == str(student_id)
    assert messages[0].payload["amount"] == "530.45"
    assert messages[0].payload["penalty_amount"] == "100.00"
    assert account["row"].overdue_notified_at is not None
    assert account["row"].late_penalty_amount == Decimal("100.00")
    assert account["row"].late_penalty_applied_at is not None
    assert (
        len(
            (
                await db.scalars(
                    select(AuditEntry).where(
                        AuditEntry.action == "billing.late_penalty_applied"
                    )
                )
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize("legacy_rate", ["0.00", "3.50", "100.00"])
async def test_ac_fin_001_7_fixed_rule_does_not_reinterpret_legacy_rate(
    db, account, legacy_rate
):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    account["fee"].late_fee_rate = Decimal(legacy_rate)
    await db.commit()
    result = await billing.read_account(db, student_id)
    assert result.netPayable == "530.45"
    assert len(await _overdue_messages(db)) == 1
    row = await db.scalar(select(StudentBillingAccount))
    assert row.late_penalty_amount == Decimal("100.00")
    assert row.fhe_subsidy_amount == Decimal("0.00")


async def test_ac_fin_001_7_9_commit_failure_rolls_back_all_effects(
    db, account, monkeypatch
):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    await db.commit()

    async def failing_commit():
        await db.flush()
        raise RuntimeError("synthetic commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(db, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            await billing.read_account(db, student_id)
    row = await db.scalar(select(StudentBillingAccount))
    assert row.overdue_notified_at is None
    assert row.late_penalty_applied_at is None
    assert row.late_penalty_amount == Decimal("0.00")
    assert row.fhe_subsidy_amount == Decimal("430.45")
    assert not await _overdue_messages(db)
    assert len((await db.scalars(select(AuditEntry))).all()) == 1
    # A real retry after rollback must persist each obligation exactly once.
    await billing.read_account(db, student_id)
    await billing.read_account(db, student_id)
    assert len(await _overdue_messages(db)) == 1
    assert len((await db.scalars(select(AuditEntry))).all()) == 3


async def test_ac_fin_001_7_paid_account_never_becomes_overdue(db, account):
    student_id = account["student"].id
    account["row"].due_date = date(2020, 1, 1)
    account["fee"].late_fee_rate = Decimal("3.50")
    await db.commit()
    result = await billing.read_account(db, student_id)
    assert result.paymentStatus == "PAID"
    assert not await _overdue_messages(db)


@pytest.mark.parametrize("coverage", [[], None])
async def test_ac_fin_001_2_empty_coverage_uses_approved_default(
    client, account, coverage
):
    account["fee"].subsidy_covered_fees = coverage
    await account["stage"]()
    response = await client.get(ACCOUNT, headers=account["headers"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert all(line["coveredByFhe"] for line in data["lines"])
    assert data["amountAssessed"] == data["subsidyApplied"] == "430.45"
    assert data["netPayable"] == "0.00"


async def test_ac_fin_001_2_7_fine_remains_noncovered_after_registrar_restores_fhe(
    db, account
):
    student_id = account["student"].id
    account["profile"].fhe_forfeited = True
    account["profile"].fhe_forfeiture_reason = "Registrar verdict"
    account["fee"].subsidy_covered_fees = []
    account["row"].due_date = date(2020, 1, 1)
    await account["stage"]()
    before = await billing.read_account(db, student_id)
    assert before.netPayable == "530.45"
    account["profile"].fhe_forfeited = False
    account["profile"].fhe_forfeiture_reason = None
    await db.commit()
    after = await billing.read_account(db, student_id)
    assert after.subsidyApplied == "430.45"
    assert after.netPayable == "100.00"
    assert after.lines[-1].coveredByFhe is False
    assert len(await _overdue_messages(db)) == 1


async def test_ac_fin_001_7_partial_payment_and_fine_settlement(db, account):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    account["row"].amount_paid = Decimal("400.00")
    await account["stage"]()
    first = await billing.read_account(db, student_id)
    assert first.netPayable == "130.45"
    account["row"].amount_paid = Decimal("530.45")
    await db.commit()
    paid = await billing.read_account(db, student_id)
    assert paid.paymentStatus == "PAID" and paid.netPayable == "0.00"
    assert not paid.holds
    assert account["row"].late_penalty_amount == Decimal("100.00")
    assert len(await _overdue_messages(db)) == 1


async def test_ac_fin_001_7_old_notice_does_not_waive_or_duplicate_new_fine(
    db, account
):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    account["row"].overdue_notified_at = datetime.now(UTC)
    await account["stage"]()
    for _ in range(2):
        result = await billing.read_account(db, student_id)
        assert result.netPayable == "530.45"
    assert not await _overdue_messages(db)
    assert account["row"].late_penalty_amount == Decimal("100.00")


async def test_ac_fin_001_7_fine_is_once_per_term_account(db, account):
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    await account["stage"]()
    await billing.read_account(db, account["student"].id)
    account["term"].is_current = False
    next_term = AcademicTerm(
        academic_year="2026-2027",
        semester="Second Semester",
        start_date=date(2027, 1, 2),
        end_date=date(2027, 6, 1),
        is_current=True,
        regular_enrollment_start=datetime.now(UTC),
        regular_enrollment_end=datetime.now(UTC) + timedelta(days=1),
        add_drop_deadline=datetime.now(UTC) + timedelta(days=7),
        grade_submission_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    db.add(next_term)
    await db.flush()
    db.add(
        FeeStructure(
            program_id=account["profile"].program_id,
            academic_term_id=next_term.id,
            tuition_per_unit=Decimal("100.11"),
        )
    )
    await db.flush()
    await billing.stage_assessment(
        db,
        student_id=account["student"].id,
        term_id=next_term.id,
        courses=[billing.AssessmentCourse(section_id=uuid.uuid4(), units=Decimal(1))],
        due_date=date(2020, 1, 1),
    )
    await db.commit()
    for _ in range(2):
        result = await billing.read_account(db, account["student"].id)
        assert result.netPayable == "200.11"
    rows = (await db.scalars(select(StudentBillingAccount))).all()
    assert len(rows) == 2
    assert all(row.late_penalty_amount == Decimal("100.00") for row in rows)
    assert len(await _overdue_messages(db)) == 2


@pytest.mark.parametrize(
    "url,permission", [(ACCOUNT, "ReadAssessment"), (PAYMENTS, "ReadPayment")]
)
async def test_ac_fin_001_1_scoped_permission_required(
    client, db, account, url, permission
):
    group = await db.scalar(select(Group).where(Group.name == "Students"))
    await iam.remove_user_from_group(db, account["student"].id, group.id)
    response = await client.get(url, headers=account["headers"])
    assert response.status_code == 403, response.text
    assert permission in response.text


@pytest.mark.parametrize("missing,expected", [(True, 404), (False, 503)])
async def test_ac_fin_001_1_missing_or_corrupt_assessment_is_not_invented(
    client, db, account, missing, expected
):
    if missing:
        await db.delete(account["row"])
    else:
        account["row"].assessment_snapshot = {"invalid": "synthetic corruption"}
    await db.commit()
    response = await client.get(ACCOUNT, headers=account["headers"])
    assert response.status_code == expected
    assert response.json()["code"] == (
        "RESOURCE_NOT_FOUND" if missing else "SERVICE_UNAVAILABLE"
    )
    assert "no-store" in response.headers["cache-control"]


async def test_ac_fin_001_7_outbox_worker_delivers_one_notice(
    db, account, drain_outbox, monkeypatch
):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cbpupsis_shared.domains.notifications.channels.send_email", sender
    )
    account["profile"].fhe_qualified = False
    account["row"].due_date = date(2020, 1, 1)
    await db.commit()
    await billing.read_account(db, account["student"].id)
    await drain_outbox()
    await drain_outbox()
    notices = (
        await db.scalars(
            select(Notification).where(
                Notification.notification_type == "billing.account_overdue"
            )
        )
    ).all()
    assert len(notices) == 1
    assert notices[0].user_id == account["student"].id
    assert sender.await_count == 1


@pytest.mark.parametrize("year_level", [1, 5, 6])
async def test_ac_fin_001_5_registrar_verdict_not_academic_year_level(
    client, db, account, year_level
):
    # Academic placement is not residency. This proves verdict consumption,
    # not the upstream Registrar's four-/five-year boundary evaluation.
    account["profile"].year_level = year_level
    account["profile"].fhe_qualified = True
    account["profile"].fhe_forfeited = False
    await db.commit()
    response = await client.get(ACCOUNT, headers=account["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["subsidyApplied"] == "430.45"
    assert response.json()["netPayable"] == "0.00"


async def test_ac_fin_001_10_fine_cannot_overflow_stored_money(db, account):
    student_id = account["student"].id
    account["profile"].fhe_qualified = False
    account["fee"].tuition_per_unit = Decimal("28571428.56")
    account["fee"].registration_fee = Decimal("0.00")
    account["fee"].lab_fee = Decimal("0.00")
    account["fee"].misc_fee = Decimal("0.00")
    account["row"].due_date = date(2020, 1, 1)
    await account["stage"]()
    with pytest.raises(BillingAccountUnavailableError):
        await billing.read_account(db, student_id)
    row = await db.scalar(select(StudentBillingAccount))
    assert row.gross_amount == Decimal("99999999.96")
    assert row.late_penalty_amount == 0 and row.late_penalty_applied_at is None
    assert not await _overdue_messages(db)


async def test_ac_fin_001_10_subsidy_withdrawal_preserves_fine_on_overflow(db, account):
    student_id = account["student"].id
    account["fee"].tuition_per_unit = Decimal("28571428.56")
    account["fee"].registration_fee = Decimal("0.00")
    account["fee"].lab_fee = Decimal("0.00")
    account["fee"].misc_fee = Decimal("0.00")
    await account["stage"]()
    row = account["row"]
    row.late_penalty_amount = Decimal("100.00")
    row.late_penalty_applied_at = datetime.now(UTC)
    await db.commit()
    account["profile"].fhe_forfeited = True
    await db.commit()
    with pytest.raises(BillingAccountUnavailableError):
        await billing.read_account(db, student_id)
    row = await db.scalar(select(StudentBillingAccount))
    assert row.fhe_subsidy_amount == Decimal("99999999.96")
    assert row.late_penalty_amount == Decimal("100.00")
    assert row.late_penalty_applied_at is not None
