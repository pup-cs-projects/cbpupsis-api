from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

from cbpupsis_core.exceptions import ConflictError, ValidationError


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


class DecisionRemarksRequiredError(ValidationError):
    code = "DECISION_REMARKS_REQUIRED"

    def __init__(self) -> None:
        super().__init__("Remarks are required when rejecting an overload request.")


class RequestAlreadyDecidedError(ConflictError):
    code = "REQUEST_ALREADY_DECIDED"

    def __init__(self) -> None:
        super().__init__("This overload request has already been decided.")
