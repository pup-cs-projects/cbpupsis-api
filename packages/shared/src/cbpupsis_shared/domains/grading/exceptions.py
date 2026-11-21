"""Opaque failures for inconsistent academic records or unavailable exports."""

from cbpupsis_core.exceptions import AppError


class GradeRecordUnavailableError(AppError):
    status_code = 503
    code = "GRADE_RECORD_UNAVAILABLE"

    def __init__(self):
        super().__init__("The academic record is temporarily unavailable.")
