"""Grade scale and academic standing vocabulary from issue #97."""

from enum import StrEnum

READ_GRADE = "ReadGrade"
READ_TRANSCRIPT = "ReadTranscript"
UPDATE_GRADE = "UpdateGrade"
SUBMIT_GRADE = "SubmitGrade"
MAX_GRADING_SHEET_BYTES = 10 * 1024 * 1024
MODERATE_GRADE = "ModerateGrade"
CREATE_INCOMPLETE_COMPLETION = "CreateIncompleteCompletion"
APPROVE_INCOMPLETE_COMPLETION = "ApproveIncompleteCompletion"

GRADE_STATE_DRAFT = "DRAFT"
GRADE_STATE_FINAL = "FINAL"

INC_STATE_PENDING = "PENDING_COMPLETION"
INC_STATE_COMPLETED = "COMPLETED"
INC_STATE_EXPIRED = "EXPIRED"

COMPLETION_STATE_PENDING_DEAN = "PENDING_DEAN"
COMPLETION_STATE_ENDORSED = "ENDORSED"
COMPLETION_STATE_REJECTED = "REJECTED"
COMPLETION_STATE_EXPIRED = "EXPIRED"

GRADE_DRAFT_SAVED_ACTION = "grading.draft_saved"
GRADES_FINALIZED_ACTION = "grading.grades_finalized"
GRADES_FINALIZED_EVENT = "grading.grades_finalized"
INC_COMPLETION_FILED_ACTION = "grading.inc_completion_filed"
INC_COMPLETION_ENDORSED_ACTION = "grading.inc_completion_endorsed"
INC_COMPLETION_REJECTED_ACTION = "grading.inc_completion_rejected"
INC_EXPIRED_ACTION = "grading.inc_expired"
INC_COMPLETION_DECIDED_EVENT = "grading.inc_completion_decided"
INC_EXPIRED_EVENT = "grading.inc_expired"

INC_EXPIRY_DAYS = 365
ENROLLED_COURSE_STATUSES = frozenset(
    {"enrolled", "enrolled_pending", "officially_enrolled"}
)

NUMERIC_COMPLETION_MARKS = frozenset(
    {"1.00", "1.25", "1.50", "1.75", "2.00", "2.25", "2.50", "2.75", "3.00"}
)
NUMERIC_MARKS = NUMERIC_COMPLETION_MARKS | {"5.00"}

GRADING_MODE_NUMERIC = "NUMERIC"
GRADING_MODE_PASS_FAIL = "PASS_FAIL"
GRADING_MODES = frozenset({GRADING_MODE_NUMERIC, GRADING_MODE_PASS_FAIL})

PASS_FAIL_MARKS = frozenset({"PASS", "FAIL"})
NUMERIC_ALLOWED_MARKS: tuple[str, ...] = (
    "1.00",
    "1.25",
    "1.50",
    "1.75",
    "2.00",
    "2.25",
    "2.50",
    "2.75",
    "3.00",
    "5.00",
    "INC",
)
PASS_FAIL_ALLOWED_MARKS: tuple[str, ...] = ("PASS", "FAIL")


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
CREATE_GRADE_CORRECTION = "CreateGradeCorrection"
READ_GRADE_CORRECTION = "ReadGradeCorrection"
APPROVE_GRADE_CORRECTION = "ApproveGradeCorrection"
