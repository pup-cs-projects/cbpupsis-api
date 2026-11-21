"""Grade scale and academic standing vocabulary from issue #97."""

from enum import StrEnum

READ_GRADE = "ReadGrade"
READ_TRANSCRIPT = "ReadTranscript"
NUMERIC_MARKS = frozenset(
    {"1.00", "1.25", "1.50", "1.75", "2.00", "2.25", "2.50", "2.75", "3.00", "5.00"}
)


class AcademicStanding(StrEnum):
    GOOD = "Good Standing"
    WARNING_1 = "Warning Level 1"
    WARNING_2 = "Warning Level 2"
    PROBATION = "Probation"
    COLLEGE_DROPPING = "College Dropping"
    UNIVERSITY_DISMISSAL = "University Dismissal"


UNIT_REDUCTIONS = {
    AcademicStanding.GOOD: 0,
    AcademicStanding.WARNING_1: 0,
    AcademicStanding.WARNING_2: 3,
    AcademicStanding.PROBATION: 6,
}
