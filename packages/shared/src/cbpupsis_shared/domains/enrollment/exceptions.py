"""Safe certificate refusals; never include token or record contents."""

from cbpupsis_core.exceptions import AppError, ConflictError


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
