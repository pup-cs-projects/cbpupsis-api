"""AC-STU-004 arithmetic against the real shared grading functions."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cbpupsis_shared.domains.grading import service
from cbpupsis_shared.domains.grading.schemas import GradeCourse


def course(mark, *, code="COMP101", units="3.0", status="Passed"):
    return GradeCourse(
        course_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        code=code,
        title="Synthetic Course",
        units=Decimal(units),
        final_grade=mark,
        status=status,
        posted_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize("nstp_code", ["NSTP", "NSTP 1", "NSTP1", "nstp_2"])
def test_ac_stu_004_2_three_unit_nstp_does_not_change_gwa(nstp_code):
    rows = [course("1.00", units="3.0"), course("2.25", units="2.0")]
    expected = service.average(*service.weighted_totals(rows))
    assert expected == Decimal("1.50")
    rows.append(course("5.00", code=nstp_code, units="3.0", status="Failed"))
    assert service.weighted_totals(rows) == (Decimal("7.50"), Decimal("5.0"))
    assert service.average(*service.weighted_totals(rows)) == expected


@pytest.mark.parametrize(
    ("mark", "status"), [("Inc", "Incomplete"), ("W", "Withdrawn")]
)
def test_ac_stu_004_2_nonnumeric_marks_are_excluded(mark, status):
    rows = [
        course(mark, status=status),
        course("3.00"),
        course("5.00", status="Failed"),
    ]
    assert service.average(*service.weighted_totals(rows)) == Decimal("4.00")


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        ("10.05", "10", "1.01"),
        ("7", "3", "2.33"),
        ("1", "1", "1.00"),
        ("5", "1", "5.00"),
    ],
)
def test_ac_stu_004_2_averages_are_decimal_and_round_half_up(
    numerator, denominator, expected
):
    result = service.average(Decimal(numerator), Decimal(denominator))
    assert isinstance(result, Decimal)
    assert result == Decimal(expected)
    assert result.as_tuple().exponent == -2


def test_ac_stu_004_3_inc_is_neither_pass_nor_fail_and_earns_no_units():
    rows = [course("Inc", status="Incomplete")]
    assert service.weighted_totals(rows) == (Decimal(0), Decimal(0))
    assert service.average(*service.weighted_totals(rows)) is None
    assert service.earned_courses(rows) == {}


def test_ac_stu_004_4_withdrawal_contributes_no_average_or_earned_units():
    rows = [course("W", status="Withdrawn"), course("2.00")]
    assert service.average(*service.weighted_totals(rows)) == Decimal("2.00")
    assert sum(service.earned_courses(rows).values()) == Decimal("3.0")


def test_ac_stu_004_6_empty_average_is_none_not_zero():
    assert service.average(*service.weighted_totals([])) is None
