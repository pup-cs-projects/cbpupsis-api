"""Stable academic enrollment, waiver, and certificate refusals.

Certificate refusals never include token or record contents.
"""

from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

from cbpupsis_core.exceptions import AppError, ConflictError, ValidationError


def _display_units(value: Decimal) -> str:
    return format(value.normalize(), "f")


class UnitCeilingExceededError(ConflictError):
    code = "ENROLL_UNIT_CEILING_EXCEEDED"

    def __init__(
        self,
        *,
        prescribed_load: Decimal,
        approved_overload_units: Decimal,
        standing_reduction: Decimal,
        effective_ceiling: Decimal,
        attempted_units: Decimal,
    ) -> None:
        units_remaining = max(effective_ceiling - attempted_units, Decimal("0"))
        super().__init__(
            "The selected load exceeds your enrollment unit ceiling. "
            f"Your prescribed load is {_display_units(prescribed_load)} units "
            f"and {_display_units(units_remaining)} units remain.",
            response_fields={
                "details": {
                    "prescribedLoad": prescribed_load,
                    "approvedOverloadUnits": approved_overload_units,
                    "standingReduction": standing_reduction,
                    "effectiveCeiling": effective_ceiling,
                    "attemptedUnits": attempted_units,
                    "unitsRemaining": units_remaining,
                }
            },
        )


class OverloadCapExceededError(ConflictError):
    code = "ENROLL_OVERLOAD_CAP_EXCEEDED"

    def __init__(self, *, maximum_overload_units: Decimal) -> None:
        super().__init__(
            "The selected load exceeds the maximum overload of "
            f"{_display_units(maximum_overload_units)} units.",
            response_fields={
                "details": {"maximumOverloadUnits": maximum_overload_units}
            },
        )


class SummerSubjectLimitError(ConflictError):
    code = "ENROLL_SUMMER_SUBJECT_LIMIT"

    def __init__(self, *, subject_cap: int) -> None:
        super().__init__(
            "Students in this admission category or taking back subjects may "
            f"enroll in at most {subject_cap} subjects during the summer term.",
            response_fields={"details": {"summerSubjectCap": subject_cap}},
        )


class ScheduleConflictError(ConflictError):
    code = "ENROLL_SCHEDULE_CONFLICT"

    def __init__(
        self,
        *,
        first_section_id: uuid.UUID,
        first_course_code: str,
        second_section_id: uuid.UUID,
        second_course_code: str,
        day: int,
        start: time,
        end: time,
    ) -> None:
        super().__init__(
            f"{first_course_code} and {second_course_code} overlap on day {day} "
            f"from {start.isoformat()} to {end.isoformat()}.",
            response_fields={
                "details": {
                    "courseCodes": [first_course_code, second_course_code],
                    "sectionIds": [str(first_section_id), str(second_section_id)],
                    "overlap": {
                        "day": day,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                }
            },
        )


class EnrollmentPeriodClosedError(ConflictError):
    code = "ENROLL_PERIOD_CLOSED"

    def __init__(self, *, start: str, end: str) -> None:
        super().__init__(
            "Enrollment is outside the configured academic period.",
            response_fields={"details": {"start": start, "end": end}},
        )


class EnrollmentFinancialHoldError(ConflictError):
    code = "ENROLL_FINANCIAL_HOLD"

    def __init__(self, *, office: str, amount: str) -> None:
        super().__init__(
            f"Resolve the financial hold with the {office} before enrolling.",
            response_fields={
                "details": {"office": office, "outstandingAmount": amount}
            },
        )


class EnrollmentHoldActiveError(ConflictError):
    code = "ENROLL_HOLD_ACTIVE"

    def __init__(self, *, office: str, hold_type: str) -> None:
        super().__init__(
            f"Resolve the {hold_type.lower()} hold with the {office} before enrolling.",
            response_fields={"details": {"office": office, "holdType": hold_type}},
        )


class EnrollmentStandingBlockedError(ConflictError):
    code = "ENROLL_STANDING_BLOCKED"

    def __init__(self, *, standing: str) -> None:
        super().__init__(f"Academic standing {standing} blocks enrollment.")


class EnrollmentSectionFullError(ConflictError):
    code = "ENROLL_SECTION_FULL"

    def __init__(self, *, section_id: uuid.UUID) -> None:
        super().__init__(
            "A selected section has no seat available.",
            response_fields={"details": {"sectionId": str(section_id)}},
        )


class OverloadNotEligibleError(ConflictError):
    code = "ENROLL_OVERLOAD_NOT_ELIGIBLE"

    def __init__(
        self, *, is_graduating: bool, gwa: Decimal | None, required_gwa: Decimal
    ) -> None:
        super().__init__(
            "An overload requires either completion of at least 85 percent of "
            "the curriculum or a previous regular-term GWA of 1.75 or better.",
            response_fields={
                "details": {
                    "isGraduating": is_graduating,
                    "gwa": gwa,
                    "requiredGwa": required_gwa,
                }
            },
        )


class AcademicRefusalError(ConflictError):
    """A structured refusal from the shared academic evaluator."""


class AcademicDataUnavailableError(ConflictError):
    code = "ENROLL_ACADEMIC_DATA_UNAVAILABLE"

    def __init__(self):
        super().__init__("Reviewed academic metadata is unavailable.")


class WaiverEligibilityError(ConflictError):
    code = "ENROLL_PREREQUISITE_NOT_MET"

    def __init__(self):
        super().__init__("No qualifying unmet prerequisite and waiver condition.")


class WaiverPendingError(ConflictError):
    code = "ENROLL_WAIVER_PENDING"

    def __init__(self):
        super().__init__("A live waiver already exists.")


class WaiverStepMismatchError(ConflictError):
    code = "ENROLL_WAIVER_STEP_MISMATCH"

    def __init__(self):
        super().__init__("Reviewer is not assigned to this step.")


class DecisionRemarksRequiredError(ValidationError):
    code = "DECISION_REMARKS_REQUIRED"

    def __init__(
        self, detail: str = "Remarks are required when rejecting a request."
    ) -> None:
        super().__init__(detail)


class RequestAlreadyDecidedError(ConflictError):
    code = "REQUEST_ALREADY_DECIDED"

    def __init__(self, detail: str = "This request has already been decided.") -> None:
        super().__init__(detail)


class VpaaReferenceRequiredError(ValidationError):
    code = "VALIDATION_ERROR"

    def __init__(self):
        super().__init__("The external VPAA approval reference is required.")


class CorNotEnrolled(ConflictError):
    code = "COR_NOT_ENROLLED"

    def __init__(self):
        super().__init__("No enrollment exists for this term.")


class CorNotFinal(ConflictError):
    code = "COR_ENROLLMENT_NOT_FINAL"

    def __init__(self, unmet=()):
        super().__init__(
            "Certificate enrollment is not final.",
            response_fields={"details": {"unmet": list(unmet)}},
        )


class CertificateUnavailable(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"

    def __init__(self):
        super().__init__("Certificate service is unavailable.")


class AddDropPeriodClosedError(ConflictError):
    code = "ENROLL_ADD_DROP_PERIOD_CLOSED"

    def __init__(self, *, period_start: str, period_end: str) -> None:
        super().__init__(
            "Add and drop period is closed.",
            response_fields={
                "details": {
                    "periodStart": period_start,
                    "periodEnd": period_end,
                }
            },
        )


class BelowMinimumLoadError(ConflictError):
    code = "ENROLL_BELOW_MINIMUM_LOAD"

    def __init__(self, *, minimum_load: Decimal, units_after: Decimal) -> None:
        super().__init__(
            f"Dropping this course takes total units ({_display_units(units_after)}) "
            f"below the minimum load of {_display_units(minimum_load)} units.",
            response_fields={
                "details": {
                    "minimumLoad": _display_units(minimum_load),
                    "unitsAfter": _display_units(units_after),
                }
            },
        )
