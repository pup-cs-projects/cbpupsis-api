"""Schedule failures use the standard API error envelope."""

from cbpupsis_core.exceptions import AppError


class ScheduleUnavailableError(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(
            "The schedule is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "30", "Cache-Control": "private, no-store"},
        )
