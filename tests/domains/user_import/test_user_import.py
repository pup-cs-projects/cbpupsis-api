"""CSV account import: the whole file, or nothing (product-owner decision 2).

Every refusal names a row, a column, and a rule, and never echoes a cell.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.models.users import AdminProfile, StudentProfile, User
from tests.conftest import temporary_password_from

IMPORT = "/api/v1/users/import"
HEADER = (
    "first_name,surname,email,role,institutional_id,birthdate,department,"
    "college,program,year_level"
)
STUDENT = "Ana,Reyes,ana@example.com,student,2024-00001-MN-0,2005-02-14,,,BSCS,1"
FACULTY = "Ben,Cruz,ben@example.com,faculty,2015-00042-MN-1,,Computer Science,,,"
FINANCE = "Dan,Lim,dan@example.com,admin_finance,,,,,,"
DEAN = "Fe,Go,fe@example.com,admin_dean,,,,CCIS,,"


def _csv(*rows: str, header: str = HEADER, bom: bool = False) -> bytes:
    text = "\r\n".join((header, *rows)) + "\r\n"
    return ("﻿" if bom else "").encode() + text.encode()


async def _upload(client: AsyncClient, headers, content: bytes):
    return await client.post(
        IMPORT, headers=headers, files={"file": ("accounts.csv", content, "text/csv")}
    )


async def _user_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(User))


@pytest.fixture
async def importing(role_groups, active_program, superadmin_headers):
    return superadmin_headers


class TestAccess:
    async def test_unauthenticated_is_401_and_another_session_is_403(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        assert (await _upload(client, {}, _csv(FINANCE))).status_code == 401
        assert (await _upload(client, auth_headers, _csv(FINANCE))).status_code == 403


class TestAValidFile:
    async def test_creates_every_row_and_mails_each_password(
        self,
        client: AsyncClient,
        db: AsyncSession,
        importing,
        drain_outbox,
        sent_emails,
    ) -> None:
        before = await _user_count(db)
        response = await _upload(
            client, importing, _csv(STUDENT, FACULTY, FINANCE, DEAN, bom=True)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"] == 4 and len(body["user_ids"]) == 4
        assert await _user_count(db) == before + 4

        student = await db.scalar(
            select(StudentProfile).where(
                StudentProfile.student_number == "2024-00001-MN-0"
            )
        )
        assert student is not None
        dean = await db.get(AdminProfile, uuid.UUID(body["user_ids"][3]))
        assert (dean.position, dean.college) == ("dean", "CCIS")

        await drain_outbox()
        assert sorted(m["to"] for m in sent_emails) == [
            "ana@example.com",
            "ben@example.com",
            "dan@example.com",
            "fe@example.com",
        ]
        login = await client.post(
            "/api/v1/auth/student/login",
            json={
                "student_number": "2024-00001-MN-0",
                "birthdate": "2005-02-14",
                "password": temporary_password_from(
                    next(m for m in sent_emails if m["to"] == "ana@example.com")
                ),
            },
        )
        assert login.status_code == 200, login.text

    async def test_header_order_and_blank_lines_do_not_matter(
        self, client: AsyncClient, importing
    ) -> None:
        header = (
            "email,role,first_name,surname,institutional_id,birthdate,department,"
            "college,program,year_level"
        )
        row = "dan@example.com,admin_finance,Dan,Lim,,,,,,"
        response = await _upload(client, importing, _csv(row, "", header=header))
        assert response.status_code == 201, response.text


class TestOneBadRowRefusesTheFile:
    async def test_nothing_is_created_and_the_row_is_named(
        self, client: AsyncClient, db: AsyncSession, importing
    ) -> None:
        before = await _user_count(db)
        bad = "Gil,Ong,not-an-email,faculty,2015-00099-MN-1,,Physics,,,"
        response = await _upload(client, importing, _csv(STUDENT, bad, FINANCE))
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "USER_IMPORT_INVALID"
        assert body["errors"] == [
            {"row": 3, "column": "email", "rule": "not a valid email address"}
        ]
        assert await _user_count(db) == before
        # Only the row, column, and rule: no cell value comes back.
        for value in ("not-an-email", "Gil", "Ong", "2015-00099-MN-1", "Physics"):
            assert value not in response.text

    async def test_every_problem_is_listed(
        self, client: AsyncClient, importing
    ) -> None:
        no_program = "Ana,Reyes,ana@example.com,student,2024-00001-MN-0,2005-02-14,,,,1"
        bad_id = "Ben,Cruz,ben@example.com,faculty,12345,,Physics,,,"
        no_college = "Fe,Go,fe@example.com,admin_dean,,,,,,"
        response = await _upload(
            client, importing, _csv(no_program, bad_id, no_college)
        )
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"row": 2, "column": "program", "rule": "required for this role"},
            {
                "row": 3,
                "column": "institutional_id",
                "rule": "must match YYYY-NNNNN-XX-N",
            },
            {"row": 4, "column": "college", "rule": "required for this role"},
        ]

    async def test_an_unknown_program_is_named(
        self, client: AsyncClient, importing
    ) -> None:
        row = STUDENT.replace("BSCS", "BSXX")
        response = await _upload(client, importing, _csv(row))
        assert response.json()["errors"] == [
            {"row": 2, "column": "program", "rule": "unknown or inactive program code"}
        ]

    async def test_a_short_row_is_named(self, client: AsyncClient, importing) -> None:
        response = await _upload(client, importing, _csv("Dan,Lim,dan@example.com"))
        assert response.status_code == 422
        assert response.json()["errors"][0]["row"] == 2


class TestDuplicates:
    async def test_a_duplicate_within_the_file(
        self, client: AsyncClient, db: AsyncSession, importing
    ) -> None:
        before = await _user_count(db)
        again = "Ann,Reyes,ANA@example.com,admin_registrar,,,,,,"
        response = await _upload(client, importing, _csv(STUDENT, FACULTY, again))
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"row": 4, "column": "email", "rule": "duplicated in this file"}
        ]
        assert await _user_count(db) == before

    async def test_a_duplicate_institutional_id_within_the_file(
        self, client: AsyncClient, importing
    ) -> None:
        twin = "Ada,Reyes,ada@example.com,student,2024-00001-MN-0,2005-03-01,,,BSCS,1"
        response = await _upload(client, importing, _csv(STUDENT, twin))
        assert response.json()["errors"] == [
            {"row": 3, "column": "institutional_id", "rule": "duplicated in this file"}
        ]

    async def test_a_duplicate_against_the_database(
        self, client: AsyncClient, db: AsyncSession, importing
    ) -> None:
        assert (await _upload(client, importing, _csv(STUDENT))).status_code == 201
        before = await _user_count(db)
        response = await _upload(client, importing, _csv(FINANCE, STUDENT))
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"row": 3, "column": "email", "rule": "already registered"},
            {"row": 3, "column": "institutional_id", "rule": "already registered"},
        ]
        assert await _user_count(db) == before


class TestFileLimits:
    async def test_a_missing_header_column(
        self, client: AsyncClient, importing
    ) -> None:
        header = HEADER.replace(",year_level", "")
        response = await _upload(
            client,
            importing,
            _csv("Dan,Lim,dan@example.com,admin_finance,,,,,", header=header),
        )
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"row": 1, "column": "year_level", "rule": "missing from the header"}
        ]

    async def test_too_many_rows(
        self, client: AsyncClient, importing, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "user_import_max_rows", 1)
        response = await _upload(client, importing, _csv(FINANCE, DEAN))
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"row": None, "column": None, "rule": "more than 1 data rows"}
        ]

    async def test_too_large(self, client: AsyncClient, importing, monkeypatch) -> None:
        monkeypatch.setattr(settings, "user_import_max_bytes", 64)
        response = await _upload(client, importing, _csv(FINANCE))
        assert response.status_code == 413
        assert response.json()["code"] == "USER_IMPORT_TOO_LARGE"

    async def test_not_utf8(self, client: AsyncClient, importing) -> None:
        response = await _upload(
            client, importing, _csv(FINANCE).replace(b"Dan", b"D\xe1n")
        )
        assert response.status_code == 422
        assert response.json()["errors"][0]["rule"] == "the file is not UTF-8 text"

    async def test_header_only(self, client: AsyncClient, importing) -> None:
        response = await _upload(client, importing, _csv())
        assert response.status_code == 422
        assert response.json()["errors"][0]["rule"] == "the file has no data rows"
