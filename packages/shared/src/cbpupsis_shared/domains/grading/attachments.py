"""Bounded content inspection for private, grading-owned document storage."""

from __future__ import annotations

import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from cbpupsis_core.exceptions import AppError
from cbpupsis_shared.domains.grading.constants import (
    MAX_GRADING_SHEET_BYTES as MAX_GRADING_SHEET_BYTES,
)


class GradingSheetTooLarge(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("The grading sheet must be at most 10 MB.")


class UnsupportedGradingSheet(AppError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"

    def __init__(self) -> None:
        super().__init__("The grading sheet content must be PDF, JPEG, or PNG.")


def inspect_grading_sheet(content: bytes) -> str:
    """Ignore filenames/MIME claims and validate the actual document structure."""
    if len(content) > MAX_GRADING_SHEET_BYTES:
        raise GradingSheetTooLarge
    try:
        if content.startswith(b"%PDF-"):
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted or not len(reader.pages):
                raise UnsupportedGradingSheet
            return "application/pdf"
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as picture:
                if picture.format not in {"PNG", "JPEG"}:
                    raise UnsupportedGradingSheet
                kind = picture.format
                picture.verify()
            with Image.open(BytesIO(content)) as picture:
                picture.load()
        return "image/png" if kind == "PNG" else "image/jpeg"
    except (
        PdfReadError,
        UnidentifiedImageError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
        EOFError,
        IndexError,
        OverflowError,
        RecursionError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise UnsupportedGradingSheet from exc
