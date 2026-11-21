"""Errors the CSV account import raises."""

from __future__ import annotations

from collections.abc import Sequence

from cbpupsis_api_admin.domains.user_import.schemas import ImportProblem
from cbpupsis_core.exceptions import AppError, ValidationError


class UserImportError(AppError):
    """Base for every error raised by the account import."""


class UserImportInvalidError(UserImportError, ValidationError):
    """The file was refused and nothing was created (HTTP 422).

    ``errors`` lists every problem found, by row and column, so the file can
    be fixed in one pass.
    """

    code = "USER_IMPORT_INVALID"

    def __init__(self, problems: Sequence[ImportProblem]) -> None:
        super().__init__(
            "The file was not imported; fix the listed rows and upload it again",
            response_fields={"errors": [p.model_dump() for p in problems]},
        )


class UserImportTooLargeError(UserImportError):
    """The upload exceeds ``USER_IMPORT_MAX_BYTES`` (HTTP 413)."""

    status_code = 413
    code = "USER_IMPORT_TOO_LARGE"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            f"The file is larger than {max_bytes} bytes",
            response_fields={"max_bytes": max_bytes},
        )
