"""Opaque integration errors, never provider payloads or credentials."""

from cbpupsis_core.exceptions import AppError


class CalendarUnavailable(AppError):
    status_code = 503
    code = "GOOGLE_CALENDAR_UNAVAILABLE"

    def __init__(self):
        super().__init__(
            "Calendar connection is unavailable. Please retry later.",
            headers={"Retry-After": "30"},
        )


class CalendarConflict(AppError):
    status_code = 409
    code = "GOOGLE_CALENDAR_CONFLICT"

    def __init__(self, detail="Check the connection status before retrying."):
        super().__init__(detail)


class InvalidOAuthState(AppError):
    status_code = 400
    code = "GOOGLE_CALENDAR_INVALID_STATE"

    def __init__(self):
        super().__init__("Authorization expired or is invalid. Start connecting again.")


class ProviderError(Exception):
    def __init__(self, code="providerUnavailable", *, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class LeaseLost(Exception):
    """A stale worker must stop without overwriting newer state."""
