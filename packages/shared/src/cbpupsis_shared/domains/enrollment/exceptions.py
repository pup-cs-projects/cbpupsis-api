from __future__ import annotations

from decimal import Decimal

from cbpupsis_core.exceptions import ConflictError


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
