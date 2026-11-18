"""Domain exceptions for grading reads and faculty grade submission."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from cbpupsis_core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    ValidationError,
)


class GradingError(AppError):
    """Base for every error raised by the grading domain."""


class GradeRecordUnavailableError(GradingError):
    status_code = 503
    code = "GRADE_RECORD_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The academic record is temporarily unavailable.")


class GradesLockedError(GradingError, ConflictError):
    """Grades for the section have already been finalized and locked (HTTP 409)."""

    code = "GRADES_LOCKED"

    def __init__(self) -> None:
        super().__init__("Grades for this section are finalized and locked.")


class GradeVersionConflictError(GradingError, ConflictError):
    """Draft submission version does not match current section version (HTTP 409)."""

    code = "VERSION_CONFLICT"

    def __init__(self, *, current_version: int) -> None:
        super().__init__(
            "Grades have changed since you last loaded them. Refresh and try again.",
            response_fields={"currentVersion": current_version},
        )


class GradeDeadlinePassedError(GradingError, ForbiddenError):
    """Final submission attempted after the term's grade deadline (HTTP 403)."""

    code = "GRADE_DEADLINE_PASSED"

    def __init__(self, *, deadline: datetime) -> None:
        super().__init__(
            "The grade submission deadline has passed. Contact your Department "
            "Chair or the Registrar's Office to request an administrative override.",
            response_fields={"deadline": deadline},
        )


class GradeValidationError(GradingError, ValidationError):
    """One or more student grade rows failed validation (HTTP 422)."""

    code = "VALIDATION_ERROR"

    def __init__(self, row_errors: Sequence[dict[str, object]]) -> None:
        errors = list(row_errors)
        if len(errors) == 1:
            row = errors[0]
            label = (
                row.get("studentNumber")
                or row.get("studentName")
                or row.get("enrollmentId")
                or row.get("studentId")
                or f"row {row.get('rowIndex', 0)}"
            )
            detail = f"Invalid grade for {label}: {row['message']}"
        else:
            labels = ", ".join(
                str(
                    row.get("studentNumber")
                    or row.get("studentName")
                    or row.get("enrollmentId")
                    or row.get("studentId")
                    or f"row {row.get('rowIndex', 0)}"
                )
                for row in errors
            )
            detail = f"Invalid grades for student rows: {labels}."
        super().__init__(
            detail,
            response_fields={
                "errors": errors,
                "details": {"rows": errors},
            },
        )
