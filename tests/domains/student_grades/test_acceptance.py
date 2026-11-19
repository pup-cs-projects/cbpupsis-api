"""Issue #146: real HTTP, IAM, repository, service and PDF acceptance coverage.

Producer limitations for AC .3/.7 are recorded in README.md. These tests do not
substitute a mocked submission/correction service for a missing implementation.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from pypdf import PdfReader
from sqlalchemy import update

from cbpupsis_database.models.grading import StudentGrade, StudentTermGPA
from cbpupsis_database.models.users import StudentProfile
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.grading import service
from cbpupsis_shared.domains.grading.constants import AcademicStanding
from tests.domains.student_grades.paths import STUDENT_RECORDS


async def get_record(client, headers, suffix="grades", **params):
    response = await client.get(
        f"{STUDENT_RECORDS}/{suffix}", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_ac_stu_004_1_latest_posted_term_has_complete_course_fields(
    client, student_headers, make_term, post_mark
):
    old, latest, draft_only = [await make_term(year) for year in (2022, 2023, 2024)]
    await post_mark(old, "3.00", code="OLD101")
    await post_mark(latest, "2.25", code="COMP101", units="4.0")
    await post_mark(draft_only, "1.00", code="DRAFT101", is_draft=True)
    record = await get_record(client, student_headers)
    assert record["term"]["id"] == str(latest.id)
    assert record["state"] == "available"
    assert len(record["courses"]) == 1
    row = record["courses"][0]
    assert {
        key: row[key] for key in ("code", "title", "units", "final_grade", "status")
    } == {
        "code": "COMP101",
        "title": "Synthetic Course",
        "units": "4.0",
        "final_grade": "2.25",
        "status": "Passed",
    }


async def test_ac_stu_004_2_http_gwa_excludes_nstp_inc_and_w(
    client, student_headers, student_account, db, make_term, post_mark
):
    term = await make_term()
    await post_mark(term, "1.00", code="COMP101", units="3.0")
    await post_mark(term, "2.25", code="COMP102", units="2.0")
    before = await get_record(client, student_headers)
    await post_mark(term, "5.00", code="NSTP1")
    await post_mark(
        term, "INC", inc_expires_at=datetime(2025, 2, 28, 8, 15, tzinfo=UTC)
    )
    await post_mark(term, "W")
    record = await get_record(client, student_headers)
    assert before["gwa"] == record["gwa"] == "1.50"
    grades = await service.read_grades(db, student_id=student_account.id)
    transcript = await service.read_transcript(db, student_id=student_account.id)
    assert isinstance(grades.gwa, Decimal)
    assert isinstance(transcript.cumulative_gwa, Decimal)
    assert all(isinstance(block.gwa, Decimal) for block in transcript.terms)


async def test_ac_stu_004_3_inc_displays_stored_365_day_deadline_and_is_neutral(
    client, student_headers, make_term, post_mark
):
    term = await make_term()
    await post_mark(term, "2.00", code="COMP101")
    # Fixed leap-day encoding and independently specified expected deadline.
    # Encoding itself belongs to the absent submission producer, not these GETs.
    await post_mark(
        term,
        "INC",
        code="COMP102",
        submitted_at=datetime(2024, 2, 29, 8, 15, tzinfo=UTC),
        inc_expires_at=datetime(2025, 2, 28, 8, 15, tzinfo=UTC),
    )
    record = await get_record(client, student_headers)
    row = next(row for row in record["courses"] if row["code"] == "COMP102")
    assert row["final_grade"] == "Inc"
    assert row["status"] == "Incomplete"
    deadline = datetime.fromisoformat(row["completion_deadline"])
    assert deadline - datetime.fromisoformat(row["posted_at"]) == timedelta(days=365)
    assert record["gwa"] == "2.00"
    assert record["units_earned"] == "3.0"


async def test_ac_stu_004_4_w_is_visible_but_earns_no_units_or_gwa(
    client, student_headers, make_term, post_mark
):
    term = await make_term()
    await post_mark(term, "W", code="COMP101")
    record = await get_record(client, student_headers)
    assert record["courses"][0]["final_grade"] == "W"
    assert record["courses"][0]["status"] == "Withdrawn"
    assert record["gwa"] is None
    assert Decimal(record["units_earned"]) == 0
    transcript = await get_record(client, student_headers, "transcript")
    assert transcript["cumulative_gwa"] is None
    assert Decimal(transcript["total_units_earned"]) == 0


async def test_ac_stu_004_5_transcript_orders_terms_and_uses_weighted_record_totals(
    client, student_headers, make_term, post_mark
):
    old, new = await make_term(2023), await make_term(2024)
    await post_mark(old, "1.00", code="COMP101", units="3.0")
    await post_mark(new, "2.25", code="COMP102", units="2.0")
    record = await get_record(client, student_headers, "transcript")
    assert [block["term"]["id"] for block in record["terms"]] == [
        str(new.id),
        str(old.id),
    ]
    assert [block["gwa"] for block in record["terms"]] == ["2.25", "1.00"]
    assert [block["courses"][0]["code"] for block in record["terms"]] == [
        "COMP102",
        "COMP101",
    ]
    assert record["cumulative_gwa"] == "1.50"
    assert Decimal(record["total_units_earned"]) == 5


async def test_ac_stu_004_6_no_grades_is_200_with_explanation_and_null_gwa(
    client, student_headers
):
    record = await get_record(client, student_headers)
    assert record["courses"] == []
    assert record["gwa"] is None
    assert record["state"] == "empty"
    assert "no finalized marks" in record["message"].lower()


async def test_ac_stu_004_7_persisted_correction_is_reread_and_gwa_recomputed(
    client, student_headers, db, student_account, make_term, post_mark
):
    """Read-side regression only, not proof of the missing approval workflow."""
    term = await make_term()
    grade, _ = await post_mark(term, "3.00", code="COMP101")
    await post_mark(term, "1.00", code="COMP102")
    assert (await get_record(client, student_headers))["gwa"] == "2.00"
    # Simulate an already committed correction without updating the loaded ORM
    # object. A stale identity-map read must not survive the next SELECT.
    await db.execute(
        update(StudentGrade)
        .where(StudentGrade.id == grade.id)
        .values(final_grade="1.50"),
        execution_options={"synchronize_session": False},
    )
    await db.commit()
    record = await get_record(client, student_headers)
    assert record["courses"][0]["final_grade"] == "1.50"
    assert record["gwa"] == "1.25"
    transcript = await service.read_transcript(db, student_id=student_account.id)
    assert transcript.cumulative_gwa == Decimal("1.25")


async def test_ac_stu_004_8_another_students_grades_are_not_found(
    client,
    student_headers,
    student_account,
    db,
    active_program,
    make_user,
    make_term,
    post_mark,
):
    other = await make_user("other-grades-student@example.com")
    db.add(
        StudentProfile(
            user_id=other.id,
            student_number="2021-00999-MN-0",
            program_id=active_program.id,
            year_level=1,
        )
    )
    await db.commit()
    term = await make_term()
    await post_mark(term, "1.00", code="OTHER101", student_id=other.id)
    record = await get_record(client, student_headers)
    transcript = await get_record(client, student_headers, "transcript")
    assert record["courses"] == []
    assert transcript["terms"] == []
    with pytest.raises(ResourceNotFoundError) as refused:
        await service.read_grades(
            db,
            student_id=student_account.id,
            student_number="2021-00999-MN-0",
        )
    assert refused.value.code == "RESOURCE_NOT_FOUND"
    # #147 removed this alias; owner scoping is proved on the real self routes
    # above, rather than inferred solely from an unregistered path returning 404.
    response = await client.get(
        "/api/v1/students/2021-00999-MN-0/grades", headers=student_headers
    )
    assert response.status_code == 404


@pytest.mark.parametrize("params", [{"year": "2024-2025"}, {"semester": "first"}])
async def test_ac_stu_004_9_unpaired_term_filters_are_refused(
    client, student_headers, params
):
    response = await client.get(
        f"{STUDENT_RECORDS}/grades", headers=student_headers, params=params
    )
    assert response.status_code == 422


async def test_ac_stu_004_9_paired_filters_select_history_not_latest(
    client, student_headers, make_term, post_mark
):
    old, new = await make_term(2023), await make_term(2024)
    await post_mark(old, "1.00")
    await post_mark(new, "3.00")
    assert (await get_record(client, student_headers))["term"]["id"] == str(new.id)
    selected = await get_record(
        client, student_headers, year=old.academic_year, semester=old.semester
    )
    assert selected["term"]["id"] == str(old.id)
    assert selected["gwa"] == "1.00"


@pytest.mark.parametrize(
    "visibility", [{"is_draft": True}, {"is_locked": False}, {"submitted_at": None}]
)
async def test_ac_stu_004_10_unpublished_marks_are_neither_shown_nor_counted(
    client, student_headers, make_term, post_mark, visibility
):
    term = await make_term()
    await post_mark(term, "2.00", code="FINAL101")
    await post_mark(term, "5.00", code="HIDDEN101", **visibility)
    grades = await get_record(client, student_headers)
    transcript = await get_record(client, student_headers, "transcript")
    assert [row["code"] for row in grades["courses"]] == ["FINAL101"]
    assert grades["gwa"] == "2.00"
    assert [row["code"] for row in transcript["terms"][0]["courses"]] == ["FINAL101"]
    assert transcript["cumulative_gwa"] == "2.00"


@pytest.mark.parametrize(
    ("standing", "reduction"),
    [
        ("Good Standing", 0),
        ("Warning Level 1", 0),
        ("Warning Level 2", 3),
        ("Probation", 6),
        ("College Dropping", None),
        ("University Dismissal", None),
    ],
)
async def test_ac_stu_004_11_stored_standing_and_reduction_are_not_guessed(
    client,
    student_headers,
    db,
    student_account,
    make_term,
    post_mark,
    standing,
    reduction,
):
    term = await make_term()
    await post_mark(term, "1.00")  # Deliberately does not imply the stored tier.
    missing = await get_record(client, student_headers)
    assert missing["academic_standing"] is None
    assert missing["unitReduction"] is None
    assert missing["standing_state"] == "not_evaluated"
    assert "not yet been evaluated" in missing["standing_message"]
    db.add(
        StudentTermGPA(
            student_id=student_account.id,
            academic_term_id=term.id,
            total_gpa_units=Decimal("3.0"),
            term_gpa=Decimal("5.00"),
            cumulative_gpa=Decimal("5.00"),
            academic_standing=standing,
        )
    )
    await db.commit()
    record = await get_record(client, student_headers)
    assert record["academic_standing"] == standing
    assert record["unitReduction"] == reduction
    assert record["standing_state"] == "evaluated"
    assert record["gwa"] == "1.00"  # Averages are not copied from the stored GPA.
    assert "Suspension" not in {tier.value for tier in AcademicStanding}


async def test_ac_stu_004_11_suspension_is_rejected_not_served_as_a_standing(
    client, student_headers, db, student_account, make_term, post_mark
):
    term = await make_term()
    await post_mark(term)
    db.add(
        StudentTermGPA(
            student_id=student_account.id,
            academic_term_id=term.id,
            total_gpa_units=Decimal("3.0"),
            term_gpa=Decimal("2.00"),
            cumulative_gpa=Decimal("2.00"),
            academic_standing="Suspension",
        )
    )
    await db.commit()
    response = await client.get(f"{STUDENT_RECORDS}/grades", headers=student_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "GRADE_RECORD_UNAVAILABLE"
    assert "Suspension" not in response.text


async def test_ac_stu_004_12_paging_defaults_and_totals_cover_the_whole_record(
    client, student_headers, make_term, post_mark
):
    for index in range(21):
        term = await make_term(1990 + index)
        await post_mark(term, "1.00" if index == 0 else "2.00", units="1.0")
    first = await get_record(client, student_headers, "transcript")
    last = await get_record(client, student_headers, "transcript", page=2)
    assert first["limit"] == 20
    assert first["total"] == last["total"] == 21
    assert len(first["terms"]) == 20 and len(last["terms"]) == 1
    assert first["nextPage"] == 2 and last["nextPage"] is None
    ids = [block["term"]["id"] for block in first["terms"] + last["terms"]]
    assert len(set(ids)) == 21
    assert last["terms"][0]["term"]["academic_year"] == "1990-1991"
    for record in (first, last):
        assert record["cumulative_gwa"] == "1.95"
        assert Decimal(record["total_units_earned"]) == 21


@pytest.mark.parametrize(
    ("limit", "status"), [(0, 422), (1, 200), (100, 200), (101, 422)]
)
async def test_ac_stu_004_12_page_size_boundaries(
    client, student_headers, limit, status
):
    response = await client.get(
        f"{STUDENT_RECORDS}/transcript",
        headers=student_headers,
        params={"limit": limit},
    )
    assert response.status_code == status
    if status == 200:
        assert response.json()["limit"] == limit


@pytest.mark.parametrize("end_offset", [0, 30])
async def test_ac_stu_004_13_unended_terms_have_gwa_but_not_cumulative_weight(
    client, student_headers, make_term, post_mark, end_offset
):
    today = datetime.now(ZoneInfo("Asia/Manila")).date()
    old = await make_term(2023)
    ongoing = await make_term(today.year, end_date=today + timedelta(days=end_offset))
    await post_mark(old, "1.00")
    await post_mark(ongoing, "3.00")
    record = await get_record(client, student_headers, "transcript")
    assert record["terms"][0]["gwa"] == "3.00"
    assert record["terms"][0]["completed"] is False
    assert record["cumulative_gwa"] == "1.00"


async def test_ac_stu_004_14_repeats_count_both_marks_but_credits_only_once(
    client, student_headers, make_term, post_mark
):
    old, new = await make_term(2023), await make_term(2024)
    _, course = await post_mark(old, "1.00", code="COMP101")
    await post_mark(new, "3.00", course=course)
    record = await get_record(client, student_headers, "transcript")
    attempts = [row for block in record["terms"] for row in block["courses"]]
    assert len(attempts) == 2
    assert {row["final_grade"] for row in attempts} == {"1.00", "3.00"}
    assert {row["course_id"] for row in attempts} == {str(course.id)}
    assert record["cumulative_gwa"] == "2.00"
    assert Decimal(record["total_units_earned"]) == 3


async def test_ac_stu_004_15_pdf_has_identity_all_terms_totals_and_page_numbers(
    client, student_headers, make_term, post_mark
):
    # More than the JSON default page proves export uses the complete record.
    for index in range(21):
        term = await make_term(1990 + index)
        await post_mark(term, "2.00", code=f"COMP{index:03}")
    response = await client.get(
        f"{STUDENT_RECORDS}/transcript/export", headers=student_headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="unofficial-transcript.pdf"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    pdf = PdfReader(BytesIO(response.content))
    assert len(pdf.pages) > 1
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert "Polytechnic University of the Philippines" in text
    assert "Unofficial Transcript - Student Copy" in text
    assert "Synthetic Grades Student" in text
    assert "2021-00146-MN-0" in text
    for index in range(21):
        assert f"COMP{index:03}" in text
        assert f"{1990 + index}-{1991 + index}" in text
    assert "Cumulative GWA (completed terms): 2.00" in text
    assert "Total units earned (unique passed courses): 63.0" in text
    for number, page in enumerate(pdf.pages, start=1):
        assert f"Page {number}" in page.extract_text()


@pytest.mark.parametrize("suffix", ["grades", "transcript", "transcript/export"])
@pytest.mark.parametrize("bad_mark", ["4.00", "NaN", "INC"])
async def test_ac_stu_004_16_invalid_records_fail_without_partial_results(
    client, student_headers, make_term, post_mark, suffix, bad_mark
):
    term = await make_term()
    await post_mark(term, "1.00", code="VALID101")
    await post_mark(
        term, bad_mark, code="INVALID101"
    )  # INC deliberately lacks deadline.
    response = await client.get(f"{STUDENT_RECORDS}/{suffix}", headers=student_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "GRADE_RECORD_UNAVAILABLE"
    assert "temporarily unavailable" in body["detail"].lower()
    assert "courses" not in body and "terms" not in body
    assert "VALID101" not in response.text
    assert not response.content.startswith(b"%PDF")


@pytest.mark.parametrize("suffix", ["grades", "transcript", "transcript/export"])
async def test_ac_stu_004_17_faculty_session_is_404_resource_not_found(
    client, faculty_headers, suffix
):
    response = await client.get(f"{STUDENT_RECORDS}/{suffix}", headers=faculty_headers)
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
